const TOKEN_KEY = "runchain_token";
let unauthorizedHandler: (() => Promise<void> | void) | undefined;

export type ApiErrorPayload = { code?: string; message?: string; request_id?: string };

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code = "REQUEST_FAILED",
    readonly requestId?: string,
  ) {
    super(message);
  }
}

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export function setUnauthorizedHandler(handler?: () => Promise<void> | void) {
  unauthorizedHandler = handler;
}

async function handleUnauthorized() {
  clearToken();
  await unauthorizedHandler?.();
}

function authorizedHeaders(headers?: HeadersInit) {
  const result = new Headers(headers);
  const token = getToken();
  if (token) result.set("Authorization", `Bearer ${token}`);
  return result;
}

async function toApiError(response: Response) {
  const payload = (await response.json().catch(() => ({}))) as ApiErrorPayload;
  return new ApiError(
    payload.message || `请求失败（${response.status}）`,
    response.status,
    payload.code,
    payload.request_id || response.headers.get("X-Request-ID") || undefined,
  );
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = authorizedHeaders(init.headers);
  if (init.body && typeof init.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    if (response.status === 401) await handleUnauthorized();
    throw await toApiError(response);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function apiStream(path: string, init: RequestInit = {}) {
  const headers = authorizedHeaders(init.headers);
  headers.set("Accept", "text/event-stream");
  if (init.body && typeof init.body === "string") headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    if (response.status === 401) await handleUnauthorized();
    throw await toApiError(response);
  }
  return response;
}
