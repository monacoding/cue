"""API routers grouped by domain (system / projects / pipeline / recipe / jobs / output).

Each module exposes a `router` (APIRouter); app/main.py wires them onto the FastAPI app.
Splitting the former 730-line main.py here keeps each concern independently navigable.
"""
