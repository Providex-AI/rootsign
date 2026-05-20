from providex.crud.base import CRUDBase
from providex.models.incident import Incident
from providex.schemas.incident import IncidentCreate


class CRUDIncident(CRUDBase[Incident, IncidentCreate]):
    pass


incident = CRUDIncident(Incident, pk_attr="incident_id")
