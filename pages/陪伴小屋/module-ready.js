/* Runs after every page module in the full-asset template has executed.
 * app.js paints the shell before the larger page modules finish loading, then
 * re-renders the current route once this marker lands. */
"use strict";
window.dispatchEvent(new Event("companion:modules-ready"));
