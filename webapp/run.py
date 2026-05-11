"""서버 실행: python -m webapp.run

환경변수:
  PORT     - 바인딩 포트 (기본 8000). Railway 등 PaaS는 자동 주입.
  RELOAD   - "1"이면 reload 활성 (기본: 로컬은 1, 운영은 0)
"""
import os
import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    reload = os.getenv("RELOAD", "0") == "1"
    uvicorn.run("webapp.main:app", host="0.0.0.0", port=port, reload=reload)
