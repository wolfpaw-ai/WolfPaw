import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthContext";
import { RequireAuth } from "./auth/RequireAuth";
import { SignInPage } from "./auth/SignInPage";
import { VerifyPage } from "./auth/VerifyPage";
import { ChatPage } from "./chat/ChatPage";
import { FilesPage } from "./files/FilesPage";
import { AppLayout } from "./layout/AppLayout";
import { ProfilePage } from "./profile/ProfilePage";
import { TaskDetailPage } from "./tasks/TaskDetailPage";
import { TasksPage } from "./tasks/TasksPage";
import { UsagePage } from "./usage/UsagePage";

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          {/* Public auth routes. */}
          <Route path="/signin" element={<SignInPage />} />
          {/* The backend's magic link points at /auth/verify?token=...
              The Caddy in production proxies /auth/* to the backend;
              this client route handles the magic link's redirect target
              when the backend hands off to the SPA via a 302 to
              /signin/verify?token=... (a small backend tweak in step 21
              wires that). For dev, paste the token into this URL. */}
          <Route path="/signin/verify" element={<VerifyPage />} />

          {/* Authenticated app. */}
          <Route element={<RequireAuth><AppLayout /></RequireAuth>}>
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/tasks/:id" element={<TaskDetailPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/usage" element={<UsagePage />} />
            <Route path="/profile" element={<ProfilePage />} />
          </Route>

          <Route path="/" element={<Navigate to="/chat" replace />} />
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  );
}
