# Licensed under the EUPL, Version 1.2 or – as soon they will be approved by
# the European Commission - subsequent versions of the EUPL (the "Licence");
# You may not use this work except in compliance with the Licence.
# You may obtain a copy of the Licence at:
# https://joinup.ec.europa.eu/software/page/eupl

import os

from dotenv import load_dotenv

load_dotenv()
load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

from fastapi import FastAPI

from app.database import Base, engine
from app.routers import (
    analytics_and_maintenance_apis,
    fetch_field,
    field_registration,
    point_registration,
    traceforward,
)

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