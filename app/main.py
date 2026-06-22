from fastapi import FastAPI
from app.database import engine, Base
from app.routers import field_registration , fetch_field , point_registration , analytics_and_maintenance_apis

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




@app.get("/")
async def root():
    return {"message": "Welcome to Asset Registry 2.0 APIs"}