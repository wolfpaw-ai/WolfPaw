import { Outlet } from "react-router-dom";
import { Nav } from "./Nav";

/** Shell wrapping every authenticated page — primary nav at top,
 *  routed page in the main area. */
export function AppLayout() {
  return (
    <div className="app-shell">
      <Nav />
      <div className="app-body">
        <Outlet />
      </div>
    </div>
  );
}
