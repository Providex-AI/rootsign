from providex.crud.base import CRUDBase
from providex.models.session import ProvidexSession
from providex.schemas.session import SessionCreate


class CRUDSession(CRUDBase[ProvidexSession, SessionCreate]):
    pass


session = CRUDSession(ProvidexSession, pk_attr="session_id")
