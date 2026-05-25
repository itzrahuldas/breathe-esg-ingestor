// src/components/Navbar.jsx
import { NavLink } from 'react-router-dom';

export default function Navbar() {
  return (
    <nav className="navbar">
      <span className="navbar-logo">🌿 Breathe <span>ESG</span></span>
      <NavLink to="/"       className={({isActive}) => 'nav-link' + (isActive ? ' active' : '')}>Dashboard</NavLink>
      <NavLink to="/upload" className={({isActive}) => 'nav-link' + (isActive ? ' active' : '')}>Upload</NavLink>
      <NavLink to="/review" className={({isActive}) => 'nav-link' + (isActive ? ' active' : '')}>Review</NavLink>
      <NavLink to="/audit"  className={({isActive}) => 'nav-link' + (isActive ? ' active' : '')}>Audit Log</NavLink>
      <span className="navbar-spacer" />
      <span className="navbar-client">Prototype · Client #{import.meta.env.VITE_CLIENT_ID || '1'}</span>
    </nav>
  );
}
