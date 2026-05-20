from providex.crud.base import CRUDBase
from providex.models.policy import Policy
from providex.schemas.policy import PolicyCreate


class CRUDPolicy(CRUDBase[Policy, PolicyCreate]):
    pass


policy = CRUDPolicy(Policy, pk_attr="policy_id")
