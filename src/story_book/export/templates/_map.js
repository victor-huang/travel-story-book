/* Draws the day's route over OpenStreetMap tiles.
 *
 * No state: nothing stored, no history pushed, the back button behaves. If the tiles cannot be
 * fetched -- offline, or the file opened from disk with no network -- Leaflet still draws the
 * route and the markers on a blank background, which is the fallback the SVG used to be. */
(function () {
  var node = document.getElementById("map-data");
  if (!node || typeof L === "undefined") return;
  var data = JSON.parse(node.textContent);
  var points = data.marks.map(function (m) { return [m.lat, m.lon]; });
  if (!points.length && !data.route.length) return;

  var map = L.map("map", { scrollWheelZoom: false, attributionControl: true });
  L.tileLayer(data.tileUrl, { maxZoom: data.maxZoom, attribution: data.attribution }).addTo(map);

  if (data.route.length > 1) {
    L.polyline(data.route, { color: "#6fb1ff", weight: 3, opacity: 0.85 }).addTo(map);
  }
  data.marks.forEach(function (m) {
    /* Interpolated fixes are hollow and dashed. They are the ones the map might be lying about,
     * so a reader has to be able to tell them from a measured position. */
    L.circleMarker([m.lat, m.lon], {
      radius: 5,
      color: "#ffd25a",
      weight: 2,
      opacity: 1,
      fillColor: "#ffd25a",
      fillOpacity: m.interpolated ? 0 : 1,
      dashArray: m.interpolated ? "2 2" : null
    }).addTo(map).bindTooltip(m.label);
  });

  var bounds = L.latLngBounds(points.concat(data.route));
  map.fitBounds(bounds, { padding: [24, 24], maxZoom: 16 });
})();
