// Bias slider — does TWO things based on the slider position (0..100):
//   1. Filters which articles are visible. The further from center, the
//      narrower the visible bias window, so sliding right gradually drops
//      the most-left articles out of view.
//   2. Swaps which variant is shown on the visible cards (left rewrite,
//      original, or right rewrite).
//
// At 50 (center): all cards visible, original framing shown.
// At 0  (full left):  only bias <= ~0.20 visible, left rewrite shown.
// At 100 (full right): only bias >= ~0.80 visible, right rewrite shown.
(function () {
    const slider = document.getElementById("bias-slider");
    const status = document.getElementById("slider-status");
    if (!slider) return;

    const cards = Array.from(document.querySelectorAll("article.card"));

    function variantForValue(v) {
        if (v < 35) return "left";
        if (v > 65) return "right";
        return "original";
    }

    // Tolerance band: wide at center (everything visible), narrowing as the
    // user moves toward the extremes. This produces a feed that "leans" with
    // the slider rather than abruptly cutting off articles at a hard threshold.
    function visibilityWindow(v) {
        const target = v / 100;
        const distFromCenter = Math.abs(target - 0.5); // 0 .. 0.5
        // 0.55 at center → effectively no filter; 0.20 at the extremes
        const tolerance = 0.55 - distFromCenter * 0.7;
        return { target, tolerance };
    }

    const VARIANT_NAME = {
        left: "Left-leaning rewrite",
        right: "Right-leaning rewrite",
        original: "Original framing",
    };

    // Nominal positions on the 0..1 bias scale that the L/R variants are
    // *written to inhabit*. The variants don't carry their own measured
    // bias score (they're rewrites, not separately analyzed), so we anchor
    // them at conventional positions for the "Current" marker.
    const VARIANT_POSITION = { left: 0.20, right: 0.80 }; // original = source bias
    const VARIANT_SHORT = { left: "left", right: "right", original: "original" };

    function statusText(name, visible, total) {
        const subject = VARIANT_NAME[name].toLowerCase();
        if (visible === total) return `Showing ${subject} for all ${total} articles`;
        return `Showing ${subject} for ${visible} of ${total} articles`;
    }

    function apply(v) {
        const variant = variantForValue(v);
        const { target, tolerance } = visibilityWindow(v);
        let visible = 0;

        cards.forEach((card) => {
            const bias = parseFloat(card.dataset.bias);
            const inWindow =
                isNaN(bias) || Math.abs(bias - target) <= tolerance;
            card.classList.toggle("filter-hidden", !inWindow);
            if (inWindow) visible += 1;

            // Swap variant visibility within each card. Even hidden cards
            // get the swap so a future show doesn't flash the wrong variant.
            card.querySelectorAll(".variant").forEach((el) => { el.hidden = true; });
            const target_el = card.querySelector(".variant-" + variant);
            if (target_el) target_el.hidden = false;

            // Update the dual-marker bias chart: leave the Source marker put,
            // move the Current marker to the variant's nominal position, and
            // refresh the legend text so the page reads:
            //   Original ●: center-left · 0.40 | Current ▼: right · 0.80
            const chart = card.querySelector(".bias-chart");
            if (chart) {
                const sourcePos = parseFloat(chart.dataset.sourcePos);
                const currentPos =
                    variant in VARIANT_POSITION
                        ? VARIANT_POSITION[variant]
                        : sourcePos;

                const currentMarker = chart.querySelector(".bias-bar-marker-current");
                if (currentMarker) {
                    currentMarker.style.left = (currentPos * 100).toFixed(1) + "%";
                }
                const currentVal = chart.querySelector(".legend-current-value");
                if (currentVal) {
                    currentVal.textContent =
                        VARIANT_SHORT[variant] + " · " + currentPos.toFixed(2);
                }
            }
        });

        if (status) status.textContent = statusText(variant, visible, cards.length);
    }

    slider.addEventListener("input", (e) => apply(parseInt(e.target.value, 10)));
    apply(parseInt(slider.value, 10));
})();
