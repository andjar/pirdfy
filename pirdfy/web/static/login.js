import { $, api } from "./app.js";

const form = $("#login");
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const setup = form.dataset.setup === "true";
  const err = $("#err");
  err.textContent = "";
  try {
    await api(setup ? "/api/auth/setup" : "/api/auth/login", { method: "POST", body: { password: $("#password").value } });
    const next = new URLSearchParams(location.search).get("next");
    location.href = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
  } catch (ex) {
    err.textContent = ex.message;
    $("#password").select();
  }
});
