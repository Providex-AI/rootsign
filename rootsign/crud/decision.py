from rootsign.crud.base import CRUDBase
from rootsign.models.decision import Decision
from rootsign.schemas.decision import DecisionCreate


class CRUDDecision(CRUDBase[Decision, DecisionCreate]):
    pass


decision = CRUDDecision(Decision, pk_attr="decision_id")
