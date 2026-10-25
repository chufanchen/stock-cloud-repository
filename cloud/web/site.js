"use strict";
const search = document.querySelector("[data-filter]");
if (search) {
  search.addEventListener("input", () => {
    const query = search.value.trim().replaceAll("-", ".");
    document.querySelectorAll("[data-filter-row]").forEach(row => {
      const day = row.getAttribute("href").replaceAll("-", ".");
      row.hidden = !day.includes(query);
    });
  });
}
