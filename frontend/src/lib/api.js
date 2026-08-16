import axios from "axios";

const backendBase = process.env.REACT_APP_BACKEND_URL || (window.location.hostname === "localhost" ? "http://localhost:8000" : "");
const API_BASE = backendBase ? `${backendBase.replace(/\/$/, "")}/api` : "/api";

export const api = axios.create({
  baseURL: API_BASE,
  withCredentials: true,
});

api.interceptors.request.use((cfg) => {
  const t = localStorage.getItem("sp_token");
  if (t) cfg.headers.Authorization = `Bearer ${t}`;
  return cfg;
});

export function fmtErr(detail) {
  if (detail == null) return "Something went wrong.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).join(" ");
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
}

export const API_URL = API_BASE;
