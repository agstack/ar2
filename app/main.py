# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

from fastapi import FastAPI
from app.database import engine, Base
from app.routers import field_registration , fetch_field , point_registration , analytics_and_maintenance_apis, traceforward

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Asset Registry 2.0",
    description="FastAPI rewrite of the Terrapipe Asset Registry",
    version="2.0.0"
)

# router
app.include_router(field_registration.router)
app.include_router(fetch_field.router)
app.include_router(point_registration.router)
app.include_router(analytics_and_maintenance_apis.router)
app.include_router(traceforward.router)




@app.get("/")
async def root():
    return {"message": "Welcome to Asset Registry 2.0 APIs"}