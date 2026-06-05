from rootsign.crud.base import CRUDBase
from rootsign.models.agent import Agent
from rootsign.schemas.agent import AgentCreate


class CRUDAgent(CRUDBase[Agent, AgentCreate]):
    pass


agent = CRUDAgent(Agent, pk_attr="agent_id")
