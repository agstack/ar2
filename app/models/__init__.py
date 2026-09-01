# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

# Import the shared Base so Alembic can access it
from app.database import Base as Base

# Import all individual models here
from .geo_id_model import GeoID as GeoID

# When you add new tables later, just add them to this list:
# from .some_other_model import SomeOtherModel