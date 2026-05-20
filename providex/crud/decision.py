from providex.crud.base import CRUDBase
from providex.models.decision import Decision
from providex.schemas.decision import DecisionCreate


class CRUDDecision(CRUDBase[Decision, DecisionCreate]):
    pass


decision = CRUDDecision(Decision, pk_attr="decision_id")
