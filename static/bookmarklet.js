(function () {
  const configuredBaseUrl = window.PACKCHECKIN_BASE_URL || "http://127.0.0.1:80";

  let targetUrl;
  try {
    targetUrl = new URL("/bookmarklet/new", configuredBaseUrl);
  } catch (error) {
    alert("Packcheckin bookmarklet is misconfigured. Set a valid PACKCHECKIN_BASE_URL.");
    return;
  }

  targetUrl.searchParams.set("link", window.location.href);

  const popup = window.open(targetUrl.toString(), "packcheckin-bookmarklet", "width=900,height=820,resizable=yes,scrollbars=yes");

  if (!popup) {
    window.location.href = targetUrl.toString();
  }
})();
