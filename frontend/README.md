# IncidentPilot — Frontend

React + Vite + TypeScript. Calls the backend via `VITE_API_URL` only — never hardcoded localhost in production code.

## Run
```bash
npm install
cp .env.example .env   # VITE_API_URL=http://localhost:8000 locally
npm run dev            # http://localhost:5173
npm run build          # production build
```

## Screens
Incident list · Incident detail (timeline, evidence, hypotheses) · Plan approve/reject/dry-run/execute/verify · Postmortem draft · Eval dashboard · Knowledge base.
