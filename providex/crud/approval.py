from providex.crud.base import CRUDBase
from providex.models.approval import Approval
from providex.schemas.approval import ApprovalCreate


class CRUDApproval(CRUDBase[Approval, ApprovalCreate]):
    pass


approval = CRUDApproval(Approval, pk_attr="approval_id")
