from rootsign.crud.base import CRUDBase
from rootsign.models.session import AgentSession
from rootsign.schemas.session import SessionCreate


class CRUDSession(CRUDBase[AgentSession, SessionCreate]):
    pass


session = CRUDSession(AgentSession, pk_attr="session_id")
