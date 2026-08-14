// ============================================================
// 全局 3D 效果：极光背景 + 漂浮光球 + 透视网格 + 粒子 + 卡片倾斜
// 所有页面共用，不依赖任何业务逻辑
// ============================================================

(function () {
  // ---------- 注入背景场景 ----------
  function buildScene() {
    if (document.querySelector(".bg-scene")) return;
    const scene = document.createElement("div");
    scene.className = "bg-scene";

    const aurora = document.createElement("div");
    aurora.className = "aurora";
    scene.appendChild(aurora);

    const orbs = [
      { c: "o1" }, { c: "o2" }, { c: "o3" },
    ];
    orbs.forEach((o) => {
      const orb = document.createElement("div");
      orb.className = "orb " + o.c;
      scene.appendChild(orb);
    });

    const grid = document.createElement("div");
    grid.className = "grid-floor";
    scene.appendChild(grid);

    const canvas = document.createElement("canvas");
    canvas.id = "bg-particles";
    scene.appendChild(canvas);

    document.body.prepend(scene);
    initParticles(canvas);
  }

  // ---------- 粒子（带 3D 深度的漂浮光点） ----------
  function initParticles(canvas) {
    const ctx = canvas.getContext("2d");
    let w, h, dots;
    const DPR = Math.min(window.devicePixelRatio || 1, 2);

    function resize() {
      w = canvas.width = window.innerWidth * DPR;
      h = canvas.height = window.innerHeight * DPR;
      canvas.style.width = window.innerWidth + "px";
      canvas.style.height = window.innerHeight + "px";
      const count = Math.min(90, Math.floor((window.innerWidth * window.innerHeight) / 16000));
      dots = Array.from({ length: count }, () => ({
        x: Math.random(),
        y: Math.random(),
        z: Math.random(), // 0(远) ~ 1(近)
        r: 0.6 + Math.random() * 1.8,
      }));
    }

    function step() {
      ctx.clearRect(0, 0, w, h);
      const colors = ["99,102,241", "139,92,246", "34,211,238"];
      for (const d of dots) {
        // 缓慢上浮
        d.y -= (0.00022 + d.z * 0.00025);
        if (d.y < -0.02) { d.y = 1.02; d.x = Math.random(); }
        const px = d.x * w;
        const py = d.y * h;
        const depth = 0.25 + d.z * 0.75;   // 近大远小
        const alpha = depth * 0.6;
        const c = colors[Math.floor(d.z * colors.length) % colors.length];
        ctx.beginPath();
        ctx.arc(px, py, d.r * depth * DPR, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(" + c + "," + alpha.toFixed(2) + ")";
        ctx.fill();
      }
      requestAnimationFrame(step);
    }

    resize();
    window.addEventListener("resize", resize);
    step();
  }

  // ---------- 卡片 3D 倾斜（鼠标跟随透视） ----------
  function initTilt() {
    const els = document.querySelectorAll(".tilt");
    els.forEach((card) => {
      card.addEventListener("mousemove", (e) => {
        const r = card.getBoundingClientRect();
        const x = (e.clientX - r.left) / r.width - 0.5;
        const y = (e.clientY - r.top) / r.height - 0.5;
        card.style.transform =
          "perspective(1000px) rotateY(" + (x * 10).toFixed(2) +
          "deg) rotateX(" + (-y * 10).toFixed(2) + "deg) translateZ(6px)";
      });
      card.addEventListener("mouseleave", () => {
        card.style.transform = "perspective(1000px) rotateX(0) rotateY(0) translateZ(0)";
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => { buildScene(); initTilt(); });
  } else {
    buildScene(); initTilt();
  }
})();
