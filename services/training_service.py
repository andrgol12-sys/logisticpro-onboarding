from database.models import TrainingResult
from database.repository import TrainingResultRepository
from schemas import TrainingAssistantTurn, TrainingResultCreate, TrainingSessionDraft


class TrainingService:
    @staticmethod
    def validate_employee_name(value: str) -> str:
        cleaned = " ".join(value.split()).strip()
        if len(cleaned) < 2:
            raise ValueError("Укажите имя сотрудника хотя бы из двух символов.")
        return cleaned

    def start_session(self, total_questions: int) -> TrainingSessionDraft:
        return TrainingSessionDraft(total_questions=total_questions)

    def register_employee_name(self, draft: TrainingSessionDraft, employee_name: str) -> TrainingSessionDraft:
        updated = TrainingSessionDraft.model_validate(draft.model_dump())
        updated.employee_name = self.validate_employee_name(employee_name)
        updated.phase = "learning"
        return updated

    def start_testing(self, draft: TrainingSessionDraft, questions: list[str]) -> TrainingSessionDraft:
        import re

        cleaned = [re.sub(r"^(?:Вопрос\s*)?\d+(?:\s*/\s*\d+)?[.):\s-]+", "", q.strip(), flags=re.I)
                   for q in questions]
        keys = ["".join(c for c in q.casefold() if c.isalnum()) for q in cleaned]
        if len(cleaned) != draft.total_questions or not all(keys) or len(set(keys)) != len(keys):
            raise ValueError("Не удалось подготовить пять разных вопросов. Напишите «Готов» ещё раз.")
        updated = draft.model_copy(deep=True)
        updated.quiz_questions = cleaned
        updated.questions_answered = 0
        updated.correct_answers = 0
        updated.phase = "testing"
        updated.current_question = cleaned[0]
        updated.asked_questions = [cleaned[0]]
        return updated

    def apply_ai_turn(
        self,
        current: TrainingSessionDraft,
        ai_turn: TrainingAssistantTurn,
    ) -> TrainingSessionDraft:
        updated = current.model_copy(deep=True)
        if current.phase != "testing":
            # Learning cannot start or complete a quiz through an AI response.
            return updated
        if current.questions_answered >= current.total_questions:
            updated.phase = "completed"
            updated.current_question = None
            return updated
        if not current.current_question or len(current.quiz_questions) != current.total_questions:
            raise ValueError("Начните тест заново командой /start.")
        if not ai_turn.latest_answer_evaluated or ai_turn.answer_is_correct is None:
            raise ValueError("Не удалось оценить ответ. Отправьте ответ ещё раз.")

        updated.questions_answered += 1
        updated.correct_answers += int(ai_turn.answer_is_correct)
        updated.last_answer_feedback = ai_turn.answer_feedback
        if updated.questions_answered == updated.total_questions:
            updated.phase = "completed"
            updated.current_question = None
            updated.final_summary = (ai_turn.final_summary or ai_turn.answer_feedback or
                                     "Тест завершён. Повторите изученный материал.")[:4000]
        else:
            updated.phase = "testing"
            updated.current_question = updated.quiz_questions[updated.questions_answered]
            updated.asked_questions.append(updated.current_question)
        return updated

    async def create_result(
        self,
        repository: TrainingResultRepository,
        draft: TrainingSessionDraft,
        topic: str,
        telegram_user_id: int,
        telegram_chat_id: int,
    ) -> TrainingResult:
        if draft.phase != "completed" or draft.questions_answered != draft.total_questions:
            raise ValueError("Нельзя сохранить незавершённый тест.")
        result_in = TrainingResultCreate(
            employee_name=draft.employee_name or "Неизвестный сотрудник",
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            topic=topic,
            total_questions=draft.total_questions,
            correct_answers=draft.correct_answers,
            score_percent=draft.score_percent(),
            final_summary=draft.final_summary,
        )
        return await repository.create(result_in)
