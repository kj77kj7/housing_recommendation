/**
 * webapp/static/config.js
 * ───────────────────────
 * 프론트엔드 환경 설정.
 *
 * - 로컬 개발 (FastAPI가 정적+API 모두 서빙): 빈 문자열 (같은 출처)
 * - 운영 (Cloudflare Pages + Railway 백엔드 분리): Railway 백엔드 URL
 *
 * localhost 에서 열면 자동으로 로컬 백엔드를 사용 — 로컬 테스트가 운영 DB 를 건드리지 않도록.
 */
window.API_BASE = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)
  ? ""
  : "https://housingrecommendation-production.up.railway.app";
