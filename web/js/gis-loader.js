// Carrega a biblioteca oficial de login do Google e avisa o app quando estiver pronta.
(function () {
  var s = document.createElement("script");
  s.src = "https://accounts.google.com/gsi/client";
  s.async = true;
  s.onload = function () { window.dispatchEvent(new Event("gis-loaded")); };
  document.head.appendChild(s);
})();
