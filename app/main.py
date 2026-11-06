from fastapi import FastAPI
from app.routers.auth import router as auth_router
from fastapi.middleware.cors import CORSMiddleware
from app.routers import memberships
from app.routers import customers
from app.routers import market_rates
from app.routers import shop_profile
from app.routers import supplier

app = FastAPI(title="Jaiwer API")

app.include_router(auth_router)
app.include_router(memberships.router)
app.include_router(customers.router)
app.include_router(market_rates.router)
app.include_router(shop_profile.router)
app.include_router(
    supplier.router,
    prefix="/api",
)

app.include_router(
    supplier.transactions_router,
    prefix="/api",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins during development
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
def health_check():
    return {"status" : "ok"}

