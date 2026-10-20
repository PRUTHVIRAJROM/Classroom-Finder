(function () {
  "use strict";

  var POLL_MS = 5000;
  var state = { rooms: [], summary: null, simulated: {}, status: "", building: "", fetchedAt: null, failed: false };

  var el = {
    count: document.getElementById("free-count"),
    heroText: document.getElementById("hero-text"),
    meta: document.getElementById("meta"),
    banner: document.getElementById("banner"),
    rooms: document.getElementById("rooms"),
    building: document.getElementById("building"),
    seg: document.querySelectorAll(".seg button"),
  };

  function h(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function minutes(sec) {
    var m = Math.floor(sec / 60);
    return m < 1 ? "under a minute" : m + " min";
  }

  function roomNumber(r) {
    return r.id.split("-").slice(1).join("-") || r.id;
  }

  function plateText(r) {
    var seats = r.capacity > 0 ? "Seats " + r.capacity : "";
    var demo = state.simulated[r.id] ? "Demo data" : "";
    var n = r.count || 0;
    var students = n + (n === 1 ? " student" : " students");
    if (r.status === "free") {
      // 0 students: plain "Free". A few students (7 or fewer): still free, but show how many.
      return { state: "Free", detail: demo || (n > 0 ? students + " spotted" : seats) };
    }
    if (r.status === "occupied") {
      return { state: "Occupied", detail: demo || students + " spotted" };
    }
    return { state: "No signal", detail: demo || seats };
  }

  function plate(r) {
    var t = plateText(r);
    var num = roomNumber(r);
    var p = h("div", "plate " + r.status);
    var top = h("div");
    top.appendChild(h("div", "num", num));
    if (r.name && r.name.toLowerCase() !== ("room " + num).toLowerCase()) {
      top.appendChild(h("div", "name", r.name));
    }
    var bottom = h("div");
    bottom.appendChild(h("div", "state", t.state));
    if (t.detail) bottom.appendChild(h("div", "detail", t.detail));
    p.appendChild(top);
    p.appendChild(bottom);
    p.setAttribute("role", "group");
    p.setAttribute("aria-label", "Room " + num + ", floor " + r.floor + ": " + t.state + (t.detail ? ", " + t.detail : ""));
    return p;
  }

  function renderHero() {
    var s = state.summary;
    if (!s) return;
    el.count.textContent = s.free;
    el.heroText.textContent = s.free === 1 ? "room free right now" : "rooms free right now";
    var bits = [s.occupied + " occupied"];
    if (s.unknown) bits.push(s.unknown + " with no signal");
    var demoCount = Object.keys(state.simulated).length;
    el.meta.textContent = bits.join(", ") + ". Updated " + ago() + "." +
      (demoCount ? " Rooms marked demo use made-up data." : "");
  }

  function ago() {
    if (!state.fetchedAt) return "never";
    var s = Math.round((Date.now() - state.fetchedAt) / 1000);
    return s < 5 ? "just now" : s + " seconds ago";
  }

  function renderBuildings() {
    var names = [];
    state.rooms.forEach(function (r) { if (names.indexOf(r.building) < 0) names.push(r.building); });
    var current = el.building.value;
    el.building.parentElement.hidden = names.length < 2;   // no picker needed for one building
    if (el.building.options.length - 1 !== names.length) {
      while (el.building.options.length > 1) el.building.remove(1);
      names.forEach(function (n) {
        var o = document.createElement("option");
        o.value = n; o.textContent = n;
        el.building.appendChild(o);
      });
      el.building.value = names.indexOf(current) >= 0 ? current : "";
      state.building = el.building.value;
    }
  }

  function renderRooms() {
    var shown = state.rooms.filter(function (r) {
      return (!state.status || r.status === state.status) && (!state.building || r.building === state.building);
    });
    el.rooms.replaceChildren();

    if (!shown.length) {
      var msg = state.rooms.length
        ? "No " + (state.status || "matching") + " rooms right now. Try another filter."
        : "No rooms loaded yet.";
      el.rooms.appendChild(h("p", "empty", msg));
      return;
    }

    var byBuilding = {};
    shown.forEach(function (r) {
      var b = byBuilding[r.building] || (byBuilding[r.building] = {});
      (b[r.floor] || (b[r.floor] = [])).push(r);
    });

    Object.keys(byBuilding).forEach(function (bn) {
      var sec = h("section", "building");
      sec.appendChild(h("h2", null, bn));
      var floors = Object.keys(byBuilding[bn]).map(Number).sort(function (a, b) { return b - a; });
      floors.forEach(function (f) {
        var row = h("div", "floor");
        row.appendChild(h("h3", null, "Floor " + f));
        var corridor = h("div", "corridor");
        byBuilding[bn][f].forEach(function (r) { corridor.appendChild(plate(r)); });
        row.appendChild(corridor);
        sec.appendChild(row);
      });
      el.rooms.appendChild(sec);
    });
  }

  function renderBanner() {
    if (state.failed) {
      el.banner.hidden = false;
      el.banner.textContent = state.fetchedAt
        ? "Can't reach the server. Showing data from " + ago() + ". Retrying every 5 seconds."
        : "Can't reach the server. Retrying every 5 seconds.";
    } else {
      el.banner.hidden = true;
    }
  }

  function render() {
    renderBuildings();
    renderHero();
    renderRooms();
    renderBanner();
  }

  function load() {
    fetch("/api/rooms", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        state.rooms = data.rooms;
        state.summary = data.summary;
        state.simulated = {};
        (data.simulated_rooms || []).forEach(function (id) { state.simulated[id] = true; });
        state.fetchedAt = Date.now();
        state.failed = false;
        render();
      })
      .catch(function () {
        state.failed = true;
        renderBanner();
        renderHero();
      });
  }

  el.seg.forEach(function (btn) {
    btn.addEventListener("click", function () {
      state.status = btn.dataset.status;
      el.seg.forEach(function (b) { b.setAttribute("aria-pressed", String(b === btn)); });
      renderRooms();
    });
  });

  el.building.addEventListener("change", function () {
    state.building = el.building.value;
    renderRooms();
  });

  load();
  setInterval(load, POLL_MS);
  setInterval(function () { if (state.summary) renderHero(); }, 1000);
})();
