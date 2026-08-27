# SPDX-License-Identifier: EUPL-1.2
# Copyright (c) 2026 AgStack project contributors.
# Licensed under the EUPL, Version 1.2; see the LICENSE file for the full text.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

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