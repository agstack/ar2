# Import the shared Base so Alembic can access it
from app.database import Base

# Import all individual models here
from .geo_id_model import GeoID

# When you add new tables later, just add them to this list:
# from .some_other_model import SomeOtherModel