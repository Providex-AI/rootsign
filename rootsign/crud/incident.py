from rootsign.crud.base import CRUDBase
from rootsign.models.incident import Incident
from rootsign.schemas.incident import IncidentCreate


class CRUDIncident(CRUDBase[Incident, IncidentCreate]):
    pass


incident = CRUDIncident(Incident, pk_attr="incident_id")
