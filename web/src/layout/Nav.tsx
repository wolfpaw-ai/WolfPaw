import { NavLink } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";

export function Nav() {
  const { state, logout } = useAuth();
  return (
    <nav className="primary-nav">
      <div className="nav-brand">
        <NavLink to="/chat">Wolfpaw</NavLink>
      </div>
      <ul className="nav-links">
        <li><NavLink to="/chat">Chat</NavLink></li>
        <li><NavLink to="/tasks">Tasks</NavLink></li>
        <li><NavLink to="/schedules">Scheduled</NavLink></li>
        <li><NavLink to="/files">Files</NavLink></li>
        <li><NavLink to="/usage">Usage</NavLink></li>
        <li><NavLink to="/monitor">Monitoring</NavLink></li>
        <li><NavLink to="/profile">Profile</NavLink></li>
      </ul>
      {state.status === "signed_in" && (
        <div className="nav-user">
          <span title={state.user.id}>{state.user.email}</span>
          <button onClick={logout} className="link-button">Sign out</button>
        </div>
      )}
    </nav>
  );
}
