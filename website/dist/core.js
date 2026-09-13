/* Tora's procedural 3D agent core. Native WebGL; no CDN, model downloads, or build tools.
   Geometry is a connected spherical node field wrapped in orbital signal paths. */
(() => {
  "use strict";
  const canvas = document.getElementById("agent-core");
  const stage = canvas?.parentElement;
  const toggle = document.getElementById("motion-toggle");
  if (!canvas || !stage || !toggle) return;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  let gl;
  try { gl = canvas.getContext("webgl", { alpha: true, antialias: true, powerPreference: "low-power" }); } catch { /* Text fallback remains visible. */ }
  if (!gl) { toggle.hidden = true; return; }
  let program;
  const buffers = [];
  function shader(type, source) {
    const result = gl.createShader(type);
    gl.shaderSource(result, source);
    gl.compileShader(result);
    if (!gl.getShaderParameter(result, gl.COMPILE_STATUS)) {
      gl.deleteShader(result);
      throw new Error("WebGL shader unavailable");
    }
    return result;
  }
  const vertex = `
    attribute vec3 aPosition;
    attribute vec4 aColor;
    attribute float aSize;
    uniform vec3 uRotation;
    uniform float uAspect;
    uniform float uDpr;
    varying vec4 vColor;
    void main() {
      vec3 p = aPosition;
      float cy = cos(uRotation.y), sy = sin(uRotation.y);
      float cx = cos(uRotation.x), sx = sin(uRotation.x);
      float cz = cos(uRotation.z), sz = sin(uRotation.z);
      p = vec3(p.x * cy + p.z * sy, p.y, -p.x * sy + p.z * cy);
      p = vec3(p.x, p.y * cx - p.z * sx, p.y * sx + p.z * cx);
      p.xy = mat2(cz, sz, -sz, cz) * p.xy;
      float perspective = 2.6 / (4.4 - p.z);
      gl_Position = vec4(p.x * perspective / uAspect, p.y * perspective, 0.0, 1.0);
      gl_PointSize = max(1.0, aSize * perspective * uDpr);
      float depth = clamp((p.z + 2.4) / 4.8, 0.0, 1.0);
      vColor = vec4(aColor.rgb, aColor.a * (0.20 + depth * 0.8));
    }`;
  const fragment = `
    precision mediump float;
    varying vec4 vColor;
    uniform bool uPoints;
    void main() {
      float opacity = vColor.a;
      if (uPoints) {
        float d = length(gl_PointCoord - vec2(0.5));
        if (d > 0.5) discard;
        opacity *= 1.0 - smoothstep(0.15, 0.5, d);
      }
      gl_FragColor = vec4(vColor.rgb, opacity);
    }`;
  try {
    program = gl.createProgram();
    const vs = shader(gl.VERTEX_SHADER, vertex), fs = shader(gl.FRAGMENT_SHADER, fragment);
    gl.attachShader(program, vs); gl.attachShader(program, fs); gl.linkProgram(program);
    gl.deleteShader(vs); gl.deleteShader(fs);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error("WebGL link unavailable");
  } catch {
    if (program) gl.deleteProgram(program);
    toggle.hidden = true;
    return;
  }
  gl.useProgram(program);
  gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE);
  const loc = {
    position: gl.getAttribLocation(program, "aPosition"),
    color: gl.getAttribLocation(program, "aColor"),
    size: gl.getAttribLocation(program, "aSize"),
    rotation: gl.getUniformLocation(program, "uRotation"),
    aspect: gl.getUniformLocation(program, "uAspect"),
    dpr: gl.getUniformLocation(program, "uDpr"),
    points: gl.getUniformLocation(program, "uPoints"),
  };
  const dots = [], lines = [], nodes = [];
  const amber = [1, .55, .29], pale = [1, .84, .64];
  let seed = 8217;
  function random() { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296; }
  function point(out, p, color, alpha = .7, size = 2) { out.push(...p, ...color, alpha, size); }
  function segment(a, b, color = amber, alpha = .22) { point(lines, a, color, alpha, 1); point(lines, b, color, alpha, 1); }
  const count = 760, golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = 1 - 2 * i / (count - 1), r = Math.sqrt(1 - y * y), angle = golden * i;
    const radius = 1.13 + .08 * Math.sin(angle * 2 + y * 6);
    const p = [Math.cos(angle) * r * radius, y * radius, Math.sin(angle) * r * radius];
    nodes.push(p);
    point(dots, p, i % 7 === 0 ? pale : amber, .45 + random() * .45, i % 23 === 0 ? 6 : 2.5);
  }
  for (let i = 0; i < nodes.length; i++) {
    let connected = 0;
    for (let j = i + 1; j < nodes.length && connected < 3; j++) {
      const a = nodes[i], b = nodes[j];
      const distance = (a[0]-b[0])**2 + (a[1]-b[1])**2 + (a[2]-b[2])**2;
      if (distance < .060) { segment(a, b, amber, .18); connected++; }
    }
  }
  // Flattened orbital ribbons rotate in three different planes.
  for (let orbit = 0; orbit < 4; orbit++) {
    const tilt = .5 + orbit * .77, radius = 1.51 + orbit * .08;
    for (let strand = 0; strand < 4; strand++) {
      let previous = null;
      for (let i = 0; i <= 280; i++) {
        const t = i / 280 * Math.PI * 2, r = radius + strand * .012;
        const x = Math.cos(t) * r, y = Math.sin(t) * r;
        const z = .05 * Math.sin(t * 3 + orbit);
        const p = [x, y * Math.cos(tilt) - z * Math.sin(tilt), y * Math.sin(tilt) + z * Math.cos(tilt)];
        if (previous) segment(previous, p, strand === 0 ? pale : amber, strand === 0 ? .39 : .17);
        if (strand === 0 && i % 35 === 0) point(dots, p, pale, .9, 5);
        previous = p;
      }
    }
  }
  // Quiet surrounding nodes give the core depth without obscuring copy.
  for (let i = 0; i < 90; i++) {
    const angle = random() * Math.PI * 2, radius = 1.85 + random() * .7;
    point(dots, [Math.cos(angle) * radius, Math.sin(angle) * radius * .7, (random()-.5)*2], pale, .18 + random()*.3, 1 + random()*2.5);
  }
  function upload(data) {
    const buffer = gl.createBuffer(); buffers.push(buffer);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(data), gl.STATIC_DRAW);
    return { buffer, count: data.length / 8 };
  }
  const lineData = upload(lines), dotData = upload(dots);
  let dpr = 1, width = 1, height = 1;
  function draw(data, mode, isPoints) {
    gl.bindBuffer(gl.ARRAY_BUFFER, data.buffer);
    gl.vertexAttribPointer(loc.position, 3, gl.FLOAT, false, 32, 0);
    gl.vertexAttribPointer(loc.color, 4, gl.FLOAT, false, 32, 12);
    gl.vertexAttribPointer(loc.size, 1, gl.FLOAT, false, 32, 28);
    gl.enableVertexAttribArray(loc.position); gl.enableVertexAttribArray(loc.color); gl.enableVertexAttribArray(loc.size);
    gl.uniform1i(loc.points, isPoints ? 1 : 0);
    gl.drawArrays(mode, 0, data.count);
  }
  let paused = reduced.matches, visible = true, lost = false, elapsed = .7, raf = 0, previous = 0;
  let targetX = 0, targetY = 0, tiltX = 0, tiltY = 0;
  function paint() {
    if (lost) return;
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT);
    gl.uniform3f(loc.rotation, .3 + tiltY, elapsed * .10 + tiltX, -.2);
    gl.uniform1f(loc.aspect, width / height); gl.uniform1f(loc.dpr, dpr);
    draw(lineData, gl.LINES, false); draw(dotData, gl.POINTS, true);
  }
  function animate(now) {
    raf = 0;
    if (paused || !visible || document.hidden || lost) { previous = 0; return; }
    if (!previous || now - previous >= 32) {
      const delta = previous ? Math.min((now - previous)/1000, .07) : 0;
      previous = now; elapsed += delta;
      tiltX += (targetX - tiltX) * .06; tiltY += (targetY - tiltY) * .06;
      paint();
    }
    raf = requestAnimationFrame(animate);
  }
  function schedule() {
    if (!raf && !paused && visible && !document.hidden && !lost) raf = requestAnimationFrame(animate);
  }
  function syncToggle() {
    toggle.setAttribute("aria-pressed", String(paused));
    toggle.setAttribute("aria-label", paused ? "Resume 3D animation" : "Pause 3D animation");
    toggle.textContent = paused ? "Resume motion ▷" : "Pause motion Ⅱ";
    schedule();
  }
  function resize() {
    width = Math.max(1, stage.clientWidth); height = Math.max(1, stage.clientHeight);
    dpr = Math.min(window.devicePixelRatio || 1, 1.6);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    gl.viewport(0, 0, canvas.width, canvas.height); paint();
  }
  toggle.addEventListener("click", () => { paused = !paused; syncToggle(); });
  reduced.addEventListener("change", e => { paused = e.matches; syncToggle(); });
  stage.addEventListener("pointermove", e => {
    if (paused || e.pointerType === "touch") return;
    const rect = stage.getBoundingClientRect();
    targetX = (e.clientX - rect.left - width/2) / width * .65;
    targetY = (e.clientY - rect.top - height/2) / height * .35;
  }, { passive: true });
  stage.addEventListener("pointerleave", () => { targetX = targetY = 0; });
  document.addEventListener("visibilitychange", schedule);
  const observer = new IntersectionObserver(entries => { visible = entries[0].isIntersecting; schedule(); }, { threshold: .01 });
  observer.observe(stage);
  const resizeObserver = new ResizeObserver(resize); resizeObserver.observe(stage);
  canvas.addEventListener("webglcontextlost", e => {
    e.preventDefault(); lost = true;
    cancelAnimationFrame(raf); raf = 0;
    stage.classList.remove("webgl-ready"); toggle.hidden = true;
  });
  window.addEventListener("pagehide", () => {
    cancelAnimationFrame(raf); observer.disconnect(); resizeObserver.disconnect();
  });
  window.addEventListener("pageshow", e => {
    if (e.persisted) { raf = 0; observer.observe(stage); resizeObserver.observe(stage); schedule(); }
  });
  resize(); stage.classList.add("webgl-ready"); syncToggle();
})();
