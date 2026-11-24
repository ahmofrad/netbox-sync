/* Local SVG chart and explicit user confirmation. No network activity. */
(function () {
  "use strict";
  var namespace = "http://www.w3.org/2000/svg";
  var circumference = 100;

  document.querySelectorAll("[data-health-chart]").forEach(function (chart) {
    var values = [
      { key: "healthy", color: "#77a18b" },
      { key: "attention", color: "#bd9662" },
      { key: "offline", color: "#b8737d" }
    ];
    var total = values.reduce(function (sum, item) {
      return sum + Math.max(0, Number(chart.dataset[item.key]) || 0);
    }, 0);
    if (!total) { return; }
    var svg = chart.querySelector("svg");
    var offset = 0;
    values.forEach(function (item) {
      var value = Math.max(0, Number(chart.dataset[item.key]) || 0);
      if (!value) { return; }
      var length = value * circumference / total;
      var segment = document.createElementNS(namespace, "circle");
      segment.setAttribute("class", "sync-donut-segment");
      segment.setAttribute("cx", "21");
      segment.setAttribute("cy", "21");
      segment.setAttribute("r", "15.9155");
      segment.setAttribute("stroke", item.color);
      segment.setAttribute("stroke-dasharray", length + " " + (circumference - length));
      segment.setAttribute("stroke-dashoffset", String(-offset));
      svg.appendChild(segment);
      offset += length;
    });
  });

  document.querySelectorAll("[data-sync-confirm]").forEach(function (button) {
    button.closest("form").addEventListener("submit", function (event) {
      if (!window.confirm(button.dataset.syncConfirm)) {
        event.preventDefault();
      }
    });
  });
}());
