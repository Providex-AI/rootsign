from rootsign.crud.base import CRUDBase
from rootsign.models.policy import Policy
from rootsign.schemas.policy import PolicyCreate


class CRUDPolicy(CRUDBase[Policy, PolicyCreate]):
    pass


policy = CRUDPolicy(Policy, pk_attr="policy_id")
