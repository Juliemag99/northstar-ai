# NorthStar AI

Private browser-based Revenue Development Platform for **NorthStar Group**.

Production target: `app.follownorthstar.com`

## Stack

| Layer | Technology |
| --- | --- |
| Frontend | React + TypeScript (Vite) |
| Backend | FastAPI |
| Database | PostgreSQL (planned) |

## Repository layout

```
northstar-ai/
├── backend/          # FastAPI API
│   └── app/main.py
├── frontend/         # React application shell + Dashboard
└── README.md
```

## Prerequisites

- Node.js 20+ and npm
- Python 3.11+
- (Optional later) PostgreSQL

## Quick start

### 1. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

API docs: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)  
Health check: [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health)

### 2. Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

App: [http://localhost:5173](http://localhost:5173)

The Vite dev server proxies `/api` and `/health` to the FastAPI backend on port `8000`.

### 3. Production build (frontend)

```bash
cd frontend
npm run build
npm run preview
```

## Milestone 1 — what’s included

- Professional NorthStar AI application shell
  - Dark navy left sidebar with branding
  - Top search / header
  - Responsive layout
- Navigation for Dashboard, Prospects, Companies, Contacts, Activities, Appointments, Campaigns, Tasks, Reports, Research, Clients, and Administration
- Dashboard with:
  - Calls Due Today
  - Follow-Ups Due
  - Appointments Today
  - Hot Prospects
  - New Assignments
  - Priority prospect list
  - Recent activity
- Working FastAPI backend health/status endpoints
- Placeholder pages for modules coming in later milestones

AI features are intentionally deferred.

## Development notes

- Dashboard data currently uses local mock data in `frontend/src/data/mockDashboard.ts` so the shell is usable before PostgreSQL is wired up.
- Keep the FastAPI backend under `backend/` — do not remove existing backend files when iterating on the frontend.
