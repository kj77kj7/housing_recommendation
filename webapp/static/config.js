/**
 * webapp/static/config.js
 * ───────────────────────
 * 프론트엔드 환경 설정.
 *
 * - 로컬 개발 (FastAPI가 정적+API 모두 서빙): 빈 문자열 (같은 출처)
 * - 운영 (Cloudflare Pages + Railway 백엔드 분리): Railway 백엔드 URL
 *
 * 배포 시:  window.API_BASE = "https://your-app.up.railway.app";
 */
window.API_BASE = "";
