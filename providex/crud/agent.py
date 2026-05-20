from providex.crud.base import CRUDBase
from providex.models.agent import Agent
from providex.schemas.agent import AgentCreate


class CRUDAgent(CRUDBase[Agent, AgentCreate]):
    pass


agent = CRUDAgent(Agent, pk_attr="agent_id")
