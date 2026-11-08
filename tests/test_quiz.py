import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from schemas import TrainingAssistantTurn
from services import TrainingService
from bot.handlers.onboarding import process_ai_training
from database import create_engine, create_session_factory, init_db, TrainingResultRepository


class State:
    def __init__(self): self.data = {}; self.cleared = False
    async def get_data(self): return self.data.copy()
    async def update_data(self, **kw): self.data.update(kw)
    async def clear(self): self.data.clear(); self.cleared = True


def turn(correct=True, evaluated=True):
    return TrainingAssistantTurn(reply='Лишний вопрос 99', phase='completed',
        latest_answer_evaluated=evaluated, answer_is_correct=correct,
        next_question='Повтор', answer_feedback='Проверено', final_summary='Итог')


class QuizTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = TrainingService()
        self.questions = [f'Тема {c}?' for c in 'АБВГД']
        self.named = self.service.register_employee_name(self.service.start_session(5), 'Анна')

    def test_all_32_scores_and_completion(self):
        for mask in range(32):
            draft = self.service.start_testing(self.named, self.questions)
            for i in range(5):
                self.assertEqual(draft.current_question, self.questions[i])
                draft = self.service.apply_ai_turn(draft, turn(bool(mask & (1 << i))))
                self.assertEqual(draft.questions_answered, i + 1)
                self.assertEqual(draft.phase, 'completed' if i == 4 else 'testing')
            self.assertEqual(draft.score_percent(), mask.bit_count() * 20)
            self.assertEqual(draft.asked_questions, self.questions)
            self.assertIsNone(draft.current_question)
            self.assertEqual(self.service.apply_ai_turn(draft, turn()), draft)

    def test_learning_and_invalid_generation(self):
        self.assertEqual(self.service.apply_ai_turn(self.named, turn()), self.named)
        with self.assertRaises(ValueError):
            self.service.start_testing(self.named, ['Вопрос 1. Дубль?'] * 5)
        draft = self.service.start_testing(self.named, self.questions)
        with self.assertRaises(ValueError): self.service.apply_ai_turn(draft, turn(evaluated=False))
        self.assertEqual(draft.questions_answered, 0)

    async def test_real_repository(self):
        engine = create_engine('sqlite+aiosqlite:///:memory:')
        try:
            await init_db(engine)
            factory = create_session_factory(engine)
            draft = self.service.start_testing(self.named, self.questions)
            for _ in range(5): draft = self.service.apply_ai_turn(draft, turn())
            async with factory() as session:
                repo = TrainingResultRepository(session)
                result = await self.service.create_result(repo, draft, 'ЛогистикПро', 1, 1)
                result_id = result.id
            async with factory() as session:
                saved = await TrainingResultRepository(session).get_by_id(result_id)
                self.assertEqual((saved.correct_answers, saved.total_questions, saved.score_percent), (5, 5, 100))
        finally: await engine.dispose()

    async def test_full_handler_and_save_retry(self):
        state = State()
        state.data = {'draft': self.service.start_session(5).model_dump(), 'name_collected': False}
        message = SimpleNamespace(text='Анна', from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1), answer=AsyncMock())
        ai = SimpleNamespace(generate_turn=AsyncMock(return_value=turn()),
                             generate_questions=AsyncMock(return_value=self.questions))
        settings = SimpleNamespace(training_topic='ЛогистикПро')
        engine = create_engine('sqlite+aiosqlite:///:memory:')
        try:
            await init_db(engine)
            factory = create_session_factory(engine)
            async def send(text):
                message.text = text
                await process_ai_training(message, state, settings, self.service, ai, factory)
            await send('Анна')
            self.assertEqual(state.data['draft']['phase'], 'learning')
            await send('Расскажи ещё')
            self.assertEqual(state.data['draft']['phase'], 'learning')
            await send('Готов')
            self.assertIn('Вопрос 1/5', message.answer.call_args.args[0])
            for i in range(4):
                await send('Ответ')
                self.assertIn(f'Вопрос {i+2}/5', message.answer.call_args.args[0])
            with patch.object(self.service, 'create_result', new=AsyncMock(side_effect=RuntimeError('DB unavailable'))):
                await send('Пятый ответ')
            self.assertEqual(state.data['draft']['questions_answered'], 5)
            calls = ai.generate_turn.await_count
            await send('Повторить сохранение')
            self.assertEqual(ai.generate_turn.await_count, calls)
            self.assertTrue(state.cleared)
            self.assertIn('100%', message.answer.call_args.args[0])
            async with factory() as session:
                from sqlalchemy import select, func
                from database.models import TrainingResult
                self.assertEqual(await session.scalar(select(func.count()).select_from(TrainingResult)), 1)
        finally: await engine.dispose()


if __name__ == '__main__': unittest.main()
