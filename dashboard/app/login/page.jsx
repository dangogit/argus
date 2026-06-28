export default async function Login({ searchParams }) {
  const params = await searchParams;
  const failed = params?.error === "1";

  return (
    <div className="login-panel">
      <h2>Open dashboard</h2>
      <form method="post" action="/api/login">
        <label htmlFor="token">Dashboard token</label>
        <input id="token" name="token" type="password" autoComplete="current-password" required />
        {failed ? <p className="form-error">Invalid dashboard token.</p> : null}
        <button type="submit">Open</button>
      </form>
    </div>
  );
}
