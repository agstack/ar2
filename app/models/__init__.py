# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# Import the shared Base so Alembic can access it
from app.database import Base

# Import all individual models here
from .geo_id_model import GeoID

# When you add new tables later, just add them to this list:
# from .some_other_model import SomeOtherModel