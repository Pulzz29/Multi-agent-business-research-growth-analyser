import { useState } from "react";
import { Eye, EyeOff, Lock, Mail } from "lucide-react";
import { ThemeToggle } from "../components/ui.jsx";
import { validateLogin } from "../lib/auth.js";

/** Email + password sign-in. Accounts are provisioned by an administrator (no sign-up). */
export function LoginPage({ onLogin, notice, theme, onToggleTheme }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [errors, setErrors] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    const found = validateLogin(email, password);
    setErrors(found);
    setError("");
    if (Object.keys(found).length) return;
    setBusy(true);
    try {
      await onLogin(email.trim(), password);
    } catch (err) {
      setError(err?.status === 401 ? "Invalid email or password." : err?.message || "Sign-in failed. Try again.");
      setBusy(false);
    }
  }

  return (
    <main className="connect">
      <div className="connect-theme">
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
      </div>
      <form className="connect-card card" onSubmit={submit} noValidate aria-busy={busy}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            IQ
          </span>
          <div>
            <p className="brand-name">InsightIQ</p>
            <p className="brand-tag">Multi-agent business intelligence</p>
          </div>
        </div>
        <h1>Sign in</h1>
        <p className="muted">Use the email and password provided by your administrator.</p>
        {notice && !error && (
          <p className="small muted" role="status">
            {notice}
          </p>
        )}
        <label className="field">
          <span>Email</span>
          <span className="input-wrap">
            <Mail size={16} aria-hidden="true" />
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="username"
              inputMode="email"
              spellCheck={false}
              placeholder="you@company.com"
              aria-invalid={Boolean(errors.email)}
              disabled={busy}
              autoFocus
            />
          </span>
          {errors.email && <span className="field-error">{errors.email}</span>}
        </label>
        <label className="field">
          <span>Password</span>
          <span className="input-wrap">
            <Lock size={16} aria-hidden="true" />
            <input
              type={visible ? "text" : "password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              spellCheck={false}
              placeholder="Password"
              aria-invalid={Boolean(errors.password)}
              disabled={busy}
            />
            <button type="button" className="icon-btn icon-btn-sm" onClick={() => setVisible((v) => !v)} aria-label={visible ? "Hide password" : "Show password"}>
              {visible ? <EyeOff size={15} /> : <Eye size={15} />}
            </button>
          </span>
          {errors.password && <span className="field-error">{errors.password}</span>}
        </label>
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
          {busy ? "Signing in…" : "Log in"}
        </button>
        <p className="small muted">Research support only. Forecasts are statistical estimates; nothing here is investment advice.</p>
      </form>
    </main>
  );
}
