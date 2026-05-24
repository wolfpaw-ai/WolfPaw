// Tiny typed fetch wrapper. Session cookie (`wp_session`) is set by the
// backend on /auth/verify and travels with every same-origin request —
// no Authorization header to manage on the client.

import type {
  Task,
  TaskDetail,
  TelegramLinkResponse,
  UsageReport,
  UsageScope,
  UserMe,
  UserProfile,
  WorkspaceFile,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown) {
    super(typeof detail === "string" ? detail : `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

async function call<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: {
      "content-type": "application/json",
      ...(init.headers || {}),
    },
  });
  if (!resp.ok) {
    let detail: unknown = await resp.text();
    try {
      detail = JSON.parse(detail as string);
      if (
        typeof detail === "object" &&
        detail !== null &&
        "detail" in detail
      ) {
        detail = (detail as { detail: unknown }).detail;
      }
    } catch {
      // keep raw text
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) {
    return undefined as unknown as T;
  }
  return (await resp.json()) as T;
}

// --- auth ---------------------------------------------------------------

export const auth = {
  async requestMagicLink(email: string): Promise<void> {
    await call<{ status: string }>("/auth/magic-link", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
  },
  async verify(token: string): Promise<{ user_id: string; created: boolean }> {
    return call("/auth/verify?token=" + encodeURIComponent(token));
  },
  async me(): Promise<UserMe> {
    return call<UserMe>("/auth/me");
  },
  async logout(): Promise<void> {
    await call<void>("/auth/logout", { method: "POST" });
  },
};

// --- tasks --------------------------------------------------------------

export const tasks = {
  async list(): Promise<Task[]> {
    const r = await call<{ tasks: Task[] }>("/tasks");
    return r.tasks;
  },
  async get(id: string): Promise<TaskDetail> {
    return call<TaskDetail>(`/tasks/${id}`);
  },
  async cancel(id: string): Promise<Task> {
    return call<Task>(`/tasks/${id}/cancel`, { method: "POST" });
  },
};

// --- usage --------------------------------------------------------------

export const usage = {
  async get(scope: UsageScope = "default"): Promise<UsageReport> {
    return call<UsageReport>(`/usage?scope=${scope}`);
  },
};

// --- workspace ----------------------------------------------------------

export const workspace = {
  async list(): Promise<WorkspaceFile[]> {
    const r = await call<{ files: WorkspaceFile[] }>("/workspace/files");
    return r.files;
  },
  async downloadUrl(id: string): Promise<{ url: string }> {
    return call<{ url: string }>(`/workspace/files/${id}/download-url`);
  },
};

// --- profile ------------------------------------------------------------

export const profile = {
  async get(): Promise<UserProfile> {
    return call<UserProfile>("/me/profile");
  },
  async patch(payload: {
    persona_md?: string;
    preferences?: Record<string, unknown>;
    timezone?: string;
  }): Promise<UserProfile> {
    return call<UserProfile>("/me/profile", {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },
};

// --- telegram -----------------------------------------------------------

export const telegram = {
  async mintLink(): Promise<TelegramLinkResponse> {
    return call<TelegramLinkResponse>(
      "/channels/telegram/link-token",
      { method: "POST", body: "{}" },
    );
  },
};

// --- ask_user reply -----------------------------------------------------

export const askUser = {
  async submit(question_id: string, answer: string): Promise<void> {
    await call<void>("/channels/web/answer", {
      method: "POST",
      body: JSON.stringify({ question_id, answer }),
    });
  },
};
