// Bias slider — swap each article's visible variant based on slider position.
// 0..34 = left, 35..65 = original, 66..100 = right.
(function () {
    const slider = document.getElementById("bias-slider");
    const status = document.getElementById("slider-status");
    if (!slider) return;

    const STATUS_TEXT = {
        left: "Showing left-leaning rewrites",
        original: "Showing original framing",
        right: "Showing right-leaning rewrites",
    };

    function variantForValue(v) {
        if (v < 35) return "left";
        if (v > 65) return "right";
        return "original";
    }

    function applyVariant(name) {
        document.querySelectorAll("article.card").forEach((card) => {
            card.querySelectorAll(".variant").forEach((v) => { v.hidden = true; });
            const target = card.querySelector(".variant-" + name);
            if (target) target.hidden = false;
        });
        if (status) status.textContent = STATUS_TEXT[name];
    }

    slider.addEventListener("input", (e) => {
        applyVariant(variantForValue(parseInt(e.target.value, 10)));
    });

    // Initialize on load (in case browser preserved a non-default value)
    applyVariant(variantForValue(parseInt(slider.value, 10)));
})();
