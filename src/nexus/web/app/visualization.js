/*
 * The scene consumes only normalized camera/selection input. Future camera or
 * gesture sources can call the returned `input` bridge (rotate/zoom/select)
 * without introducing a second scene or state authority.
 */
import * as THREE from "three";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const COLORS = {
  core: 0xbfe6ff,
  coreDeep: 0x5f9dff,
  online: 0x63e6d2,
  active: 0x79b8ff,
  awaiting: 0xffc857,
  failed: 0xff5f6d,
  unknown: 0xb388ff,
  offline: 0x596170,
  muted: 0x8b93a1,
};

const TASK_ACTIVE = new Set(["queued", "awaiting_approval", "dispatching", "sent", "running", "preparing", "stopping", "waiting"]);
const TASK_FAILED = new Set(["failed", "timed_out", "cancelled", "expired"]);
const TERMINAL_TASK_WINDOW_MS = 30 * 60 * 1000;
const TASK_MARKER_LIMIT = 12;

const PARTICLE_VERTEX = `
attribute float aScale;
attribute float aPhase;
uniform float uTime;
uniform float uSize;
uniform float uPixelRatio;
varying float vAlpha;
void main() {
  vec3 p = position;
  float breathe = 0.93 + 0.07 * sin(uTime * 1.15 + aPhase);
  p *= breathe;
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  gl_PointSize = uSize * aScale * uPixelRatio * (260.0 / max(1.0, -mv.z));
  gl_PointSize *= 0.78 + 0.38 * sin(uTime * 1.7 + aPhase);
  gl_Position = projectionMatrix * mv;
  vAlpha = 0.62 + 0.38 * sin(uTime * 1.05 + aPhase);
}`;

const PARTICLE_FRAGMENT = `
uniform vec3 uColor;
uniform float uOpacity;
varying float vAlpha;
void main() {
  vec2 c = gl_PointCoord - 0.5;
  float d = length(c);
  if (d > 0.5) discard;
  float a = smoothstep(0.5, 0.015, d);
  gl_FragColor = vec4(uColor, a * uOpacity * vAlpha);
}`;

let state = null;

function hash(value) {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function taskList(tasks) {
  return [...tasks.values()].map((value) => value.task || value).filter((task) => task?.task_id);
}

function taskVisible(task, now = Date.now()) {
  if (TASK_ACTIVE.has(task.status)) return true;
  if (task.status !== "failed" && task.status !== "unknown") return false;
  const created = Date.parse(task.created_at || "");
  return Number.isFinite(created) && now - created <= TERMINAL_TASK_WINDOW_MS;
}

function visibleTaskList(tasks, now = Date.now()) {
  const unique = new Map();
  for (const task of taskList(tasks)) {
    if (!unique.has(task.task_id) && taskVisible(task, now)) unique.set(task.task_id, task);
  }
  return [...unique.values()];
}

function taskColor(task) {
  if (!task) return COLORS.muted;
  if (task.status === "awaiting_approval") return COLORS.awaiting;
  if (task.status === "unknown") return COLORS.unknown;
  if (TASK_FAILED.has(task.status)) return COLORS.failed;
  if (TASK_ACTIVE.has(task.status)) return COLORS.active;
  return COLORS.online;
}

function makeCountSprite(value) {
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 64;
  const context = canvas.getContext("2d");
  context.fillStyle = "rgba(0,0,0,0.65)";
  context.beginPath();
  context.roundRect?.(8, 8, 112, 48, 14);
  if (context.roundRect) context.fill();
  else context.fillRect(8, 8, 112, 48);
  context.font = "600 28px system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillStyle = "#ff8b93";
  context.fillText("+" + value, 64, 33);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return new THREE.Sprite(new THREE.SpriteMaterial({
    map: texture, transparent: true, depthWrite: false, opacity: 0.9,
  }));
}

function makeGlowTexture() {
  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const context = canvas.getContext("2d");
  const gradient = context.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  gradient.addColorStop(0, "rgba(255,255,255,0.98)");
  gradient.addColorStop(0.18, "rgba(210,235,255,0.55)");
  gradient.addColorStop(0.55, "rgba(120,170,255,0.12)");
  gradient.addColorStop(1, "rgba(0,0,0,0)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function seededRandom(seed) {
  let value = seed >>> 0;
  return () => {
    value = (value * 1664525 + 1013904223) >>> 0;
    return value / 4294967296;
  };
}

function nodeLayout(nodes) {
  const count = nodes.length;
  const result = new Map();
  nodes.forEach((node, index) => {
    const angle = count === 1 ? 0.35 : -Math.PI / 2 + (index / count) * Math.PI * 2 + 0.3;
    const radius = count === 1 ? 3.15 : 3.15 + (index % 2) * 0.18;
    const y = count === 1 ? 0.12 : Math.sin(index * 2.1 + 0.6) * 0.95 + (index - (count - 1) / 2) * 0.45;
    result.set(node.node_id, new THREE.Vector3(Math.cos(angle) * radius, y, Math.sin(angle) * radius));
  });
  return result;
}

function spiralGeometry(count, arms, minRadius, maxRadius, thickness, seed) {
  const random = seededRandom(seed);
  const positions = new Float32Array(count * 3);
  const scales = new Float32Array(count);
  const phases = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    const arm = i % arms;
    const t = (i / count) + random() * 0.018;
    const angle = (arm / arms) * Math.PI * 2 + t * 4.6 + (random() - 0.5) * 0.16;
    const radius = minRadius + (maxRadius - minRadius) * t + (random() - 0.5) * 0.08;
    positions[i * 3] = Math.cos(angle) * radius;
    positions[i * 3 + 1] = Math.sin(t * Math.PI * 2 + arm) * thickness + (random() - 0.5) * thickness;
    positions[i * 3 + 2] = Math.sin(angle) * radius;
    scales[i] = 0.55 + random() * 0.95;
    phases[i] = random() * Math.PI * 2;
  }
  return { positions, scales, phases };
}

function shellGeometry(count, minRadius, maxRadius, seed) {
  const random = seededRandom(seed);
  const positions = new Float32Array(count * 3);
  const scales = new Float32Array(count);
  const phases = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    const u = random() * 2 - 1;
    const angle = random() * Math.PI * 2;
    const radius = minRadius + random() * (maxRadius - minRadius);
    const s = Math.sqrt(1 - u * u);
    positions[i * 3] = radius * s * Math.cos(angle);
    positions[i * 3 + 1] = radius * u;
    positions[i * 3 + 2] = radius * s * Math.sin(angle);
    scales[i] = 0.5 + random() * 0.9;
    phases[i] = random() * Math.PI * 2;
  }
  return { positions, scales, phases };
}

function torusGeometry(count, radius, thickness, seed) {
  const random = seededRandom(seed);
  const positions = new Float32Array(count * 3);
  const scales = new Float32Array(count);
  const phases = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    const angle = random() * Math.PI * 2;
    const r = radius + (random() - 0.5) * thickness;
    positions[i * 3] = Math.cos(angle) * r;
    positions[i * 3 + 1] = (random() - 0.5) * thickness * 0.35;
    positions[i * 3 + 2] = Math.sin(angle) * r;
    scales[i] = 0.4 + random() * 0.9;
    phases[i] = random() * Math.PI * 2;
  }
  return { positions, scales, phases };
}

function disposeObject(object) {
  if (!object) return;
  object.traverse((child) => {
    child.geometry?.dispose?.();
    if (Array.isArray(child.material)) child.material.forEach((material) => material.dispose?.());
    else child.material?.dispose?.();
  });
}

function text(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

export function initVisualization(container) {
  if (!container) return null;
  if (!window.WebGLRenderingContext || !window.WebGL2RenderingContext) {
    container.append(Object.assign(document.createElement("p"), { className: "viz-fallback", textContent: "当前环境不支持 WebGL 3D 视图。" }));
    return null;
  }

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
  } catch {
    container.append(Object.assign(document.createElement("p"), { className: "viz-fallback", textContent: "当前环境无法初始化 3D 视图。" }));
    return null;
  }

  const mobile = window.matchMedia("(max-width: 760px)").matches || (navigator.hardwareConcurrency || 8) <= 4;
  const quality = {
    mobile,
    pixelRatio: Math.min(window.devicePixelRatio || 1, mobile ? 1.25 : 2),
    coreParticles: mobile ? 1000 : 2400,
    coreHalo: mobile ? 420 : 900,
    nodeParticles: mobile ? 90 : 190,
    stars: mobile ? 320 : 760,
    bloomStrength: mobile ? 0.55 : 0.75,
    bloomRadius: mobile ? 0.4 : 0.58,
    bloomThreshold: mobile ? 0.28 : 0.22,
  };
  renderer.setPixelRatio(quality.pixelRatio);
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  container.append(renderer.domElement);
  renderer.domElement.setAttribute("aria-label", "NEXUS Core 与 Node 三维能量图");
  renderer.domElement.setAttribute("role", "img");

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x000000, 0.013);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 90);
  const coreGroup = new THREE.Group();
  const world = new THREE.Group();
  scene.add(coreGroup, world);

  const glowTexture = makeGlowTexture();
  const particleMaterials = [];
  const selectable = [];
  const nodeGroups = new Map();
  const taskGroups = new Map();
  const links = [];
  const taskFlows = [];
  const nodeSpins = new Map();
  const taskSpins = new Map();
  const taskAngles = new Map();
  let messageFlow = null;
  let messageKey = "";
  let messageDoneKey = "";
  let messageExpires = 0;
  const pointer = new THREE.Vector2();
  const raycaster = new THREE.Raycaster();
  const pointers = new Map();
  const target = new THREE.Vector3(0, 0, 0);
  const desiredTarget = new THREE.Vector3(0, 0, 0);
  const selected = { object: null, entity: null };
  let visible = true;
  let width = 1;
  let height = 1;
  let distance = 7.4;
  let desiredDistance = 7.4;
  let theta = 0.42;
  let phi = 1.22;
  let thetaVelocity = 0;
  let phiVelocity = 0;
  let dragging = false;
  let moved = 0;
  let lastX = 0;
  let lastY = 0;
  let pinchDistance = 0;
  let lastInteraction = performance.now();
  let lastSignature = "";
  let frame = 0;

  function particleMaterial(color, opacity, size) {
    const material = new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uColor: { value: new THREE.Color(color) },
        uOpacity: { value: opacity },
        uPixelRatio: { value: quality.pixelRatio },
        uSize: { value: size },
      },
      vertexShader: PARTICLE_VERTEX,
      fragmentShader: PARTICLE_FRAGMENT,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    particleMaterials.push(material);
    return material;
  }

  function pointsFrom(geometry, color, size, opacity) {
    const data = new THREE.BufferGeometry();
    data.setAttribute("position", new THREE.BufferAttribute(geometry.positions, 3));
    data.setAttribute("aScale", new THREE.BufferAttribute(geometry.scales, 1));
    data.setAttribute("aPhase", new THREE.BufferAttribute(geometry.phases, 1));
    const points = new THREE.Points(data, particleMaterial(color, opacity, size));
    points.frustumCulled = false;
    return points;
  }

  const composer = (() => {
    try {
      const value = new EffectComposer(renderer);
      value.setPixelRatio(quality.pixelRatio);
      value.addPass(new RenderPass(scene, camera));
      const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), quality.bloomStrength, quality.bloomRadius, quality.bloomThreshold);
      value.addPass(bloom);
      value.addPass(new OutputPass());
      return { value, bloom };
    } catch {
      return null;
    }
  })();

  function setCamera() {
    const sin = Math.sin(phi);
    camera.position.set(
      target.x + distance * sin * Math.sin(theta),
      target.y + distance * Math.cos(phi),
      target.z + distance * sin * Math.cos(theta),
    );
    camera.lookAt(target);
  }

  function resize() {
    width = Math.max(1, container.clientWidth);
    height = Math.max(1, container.clientHeight);
    renderer.setSize(width, height, false);
    renderer.domElement.style.width = "100%";
    renderer.domElement.style.height = "100%";
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    if (composer) {
      composer.value.setPixelRatio(quality.pixelRatio);
      composer.value.setSize(width, height);
    }
  }

  function addStars() {
    const random = seededRandom(991);
    const layers = [
      { count: quality.stars, radiusMin: 10, radiusMax: 34, size: 0.022, opacity: 0.5 },
      { count: Math.floor(quality.stars * 0.45), radiusMin: 5, radiusMax: 12, size: 0.035, opacity: 0.28 },
    ];
    for (const layer of layers) {
      const positions = new Float32Array(layer.count * 3);
      for (let i = 0; i < layer.count; i += 1) {
        const radius = layer.radiusMin + random() * (layer.radiusMax - layer.radiusMin);
        const u = random() * 2 - 1;
        const angle = random() * Math.PI * 2;
        const s = Math.sqrt(1 - u * u);
        positions[i * 3] = radius * s * Math.cos(angle);
        positions[i * 3 + 1] = radius * u;
        positions[i * 3 + 2] = radius * s * Math.sin(angle);
      }
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const material = new THREE.PointsMaterial({ color: 0x9ba8bd, size: layer.size, map: glowTexture, transparent: true, opacity: layer.opacity, depthWrite: false });
      const points = new THREE.Points(geometry, material);
      points.userData.drift = 0.00002 + random() * 0.00004;
      scene.add(points);
    }
  }
  addStars();

  function addNebula() {
    const random = seededRandom(404);
    const colors = [0x17305f, 0x272051, 0x143f4a, 0x251c3b];
    for (let i = 0; i < 4; i += 1) {
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTexture,
        color: colors[i],
        transparent: true,
        opacity: 0.1 + random() * 0.08,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }));
      const angle = (i / 4) * Math.PI * 2 + random();
      const radius = 5 + random() * 4;
      sprite.position.set(Math.cos(angle) * radius, (random() - 0.5) * 4.5, Math.sin(angle) * radius);
      sprite.scale.setScalar(9 + random() * 7);
      scene.add(sprite);
    }
  }
  addNebula();

  function buildCore() {
    const inner = pointsFrom(spiralGeometry(quality.coreParticles, 4, 0.1, 0.98, 0.18, 31), COLORS.core, 0.038, 0.88);
    const disk = pointsFrom(torusGeometry(Math.floor(quality.coreParticles * 0.55), 0.38, 0.34, 53), 0x8cc7ff, 0.034, 0.92);
    const vortex = pointsFrom(torusGeometry(Math.floor(quality.coreParticles * 0.65), 0.46, 0.3, 37), COLORS.coreDeep, 0.034, 0.72);
    const outer = pointsFrom(torusGeometry(Math.floor(quality.coreParticles * 0.8), 1.02, 0.26, 73), COLORS.coreDeep, 0.03, 0.55);
    const halo = pointsFrom(shellGeometry(quality.coreHalo, 0.65, 1.45, 51), COLORS.coreDeep, 0.026, 0.14);
    const innerGroup = new THREE.Group();
    const diskGroup = new THREE.Group();
    const vortexGroup = new THREE.Group();
    const outerGroup = new THREE.Group();
    innerGroup.add(inner);
    diskGroup.add(disk);
    diskGroup.rotation.set(1.18, 0.1, 0.3);
    vortexGroup.add(vortex);
    outerGroup.add(outer, halo);
    innerGroup.rotation.set(0.24, 0.1, -0.15);
    vortexGroup.rotation.set(0.8, 0.2, 0.25);
    outerGroup.rotation.set(0.7, 0.1, 0.22);
    coreGroup.add(innerGroup, diskGroup, vortexGroup, outerGroup);
    const nucleus = new THREE.Mesh(
      new THREE.IcosahedronGeometry(0.065, 3),
      new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.68 }),
    );
    coreGroup.add(nucleus);
    const corona = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture, color: COLORS.core, transparent: true, opacity: 0.16,
      depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    corona.scale.set(2.4, 2.4, 1);
    coreGroup.add(corona);
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(1.12, 0.006, 6, 128),
      new THREE.MeshBasicMaterial({ color: COLORS.core, transparent: true, opacity: 0.14, depthWrite: false }),
    );
    ring.rotation.x = Math.PI / 2.4;
    coreGroup.add(ring);
    const hit = new THREE.Mesh(
      new THREE.SphereGeometry(1.0, 20, 16),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }),
    );
    hit.userData.entity = { kind: "core" };
    hit.userData.highlight = coreGroup;
    coreGroup.add(hit);
    selectable.push(hit);
    coreGroup.userData = { innerGroup, diskGroup, vortexGroup, outerGroup, corona, nucleus, ring, hit };
  }
  buildCore();

  function curveFor(start, end, seed) {
    const direction = end.clone().normalize();
    const side = new THREE.Vector3(-direction.z, 0.6, direction.x).normalize();
    const random = seededRandom(seed);
    const c1 = direction.clone().multiplyScalar(1.0 + random() * 0.55).add(side.clone().multiplyScalar((random() - 0.5) * 1.25));
    const c2 = end.clone().multiplyScalar(0.48 + random() * 0.22).add(side.clone().multiplyScalar((random() - 0.5) * 0.9));
    c1.y += (random() - 0.5) * 0.9;
    c2.y += (random() - 0.5) * 0.7;
    return new THREE.CatmullRomCurve3([start, c1, c2, end], false, "catmullrom", 0.55);
  }

  function addLink(nodeId, end, color, seed) {
    const start = end.clone().normalize().multiplyScalar(0.62);
    const curve = curveFor(start, end, seed);
    const count = quality.mobile ? 35 : 70;
    const positions = new Float32Array(count * 3);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    const material = new THREE.PointsMaterial({
      color, size: quality.mobile ? 0.07 : 0.085, map: glowTexture, transparent: true, opacity: 0.95,
      depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true,
    });
    const flow = new THREE.Points(geometry, material);
    flow.frustumCulled = false;
    world.add(flow);
    const random = seededRandom(seed + 17);
    const scatter = Array.from({ length: count }, () => new THREE.Vector3(
      (random() - 0.5) * 0.15, (random() - 0.5) * 0.15, (random() - 0.5) * 0.15,
    ));
    links.push({ nodeId, curve, flow, count, offset: (hash(nodeId) % 1000) / 1000, color, speed: 0.14, scatter });
  }

  function addNode(node, position, tasks) {
    const color = node.online ? COLORS.online : COLORS.offline;
    const group = new THREE.Group();
    group.position.copy(position);
    group.userData.entity = { kind: "node", node };
    const shell = pointsFrom(shellGeometry(quality.nodeParticles, 0.2, 0.46, hash(node.node_id)), color, 0.075, node.online ? 0.85 : 0.28);
    const orbit = pointsFrom(torusGeometry(Math.floor(quality.nodeParticles * 0.65), 0.5, 0.22, hash(node.node_id) + 7), color, 0.06, node.online ? 0.65 : 0.2);
    const orbitGroup = new THREE.Group();
    orbitGroup.add(orbit);
    const nodeSpin = nodeSpins.get(node.node_id) || { shellX: 0, shellY: 0, orbitY: 0.2, orbitZ: 0.4 };
    shell.rotation.set(nodeSpin.shellX, nodeSpin.shellY, 0);
    orbitGroup.rotation.set(0.7, nodeSpin.orbitY, nodeSpin.orbitZ);
    group.add(shell, orbitGroup);
    const nucleus = new THREE.Mesh(
      new THREE.SphereGeometry(0.07, 16, 12),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity: node.online ? 0.95 : 0.45 }),
    );
    group.add(nucleus);
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture, color, transparent: true, opacity: node.online ? 0.34 : 0.14,
      depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    glow.scale.set(1.45, 1.45, 1);
    group.add(glow);
    const hit = new THREE.Mesh(
      new THREE.SphereGeometry(0.55, 18, 14),
      new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }),
    );
    hit.userData.entity = { kind: "node", node };
    hit.userData.highlight = group;
    group.add(hit);
    selectable.push(hit);
    world.add(group);
    nodeGroups.set(node.node_id, { group, shell, orbitGroup, glow, hit, position });

    const allNodeTasks = tasks
      .filter((task) => task.node_id === node.node_id)
      .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
    const extra = Math.max(0, allNodeTasks.length - TASK_MARKER_LIMIT);
    const nodeTasks = allNodeTasks.slice(-TASK_MARKER_LIMIT);
    if (extra > 0) {
      const badge = makeCountSprite(extra);
      badge.position.set(0.02, 0.78, 0);
      badge.scale.set(0.55, 0.28, 1);
      group.add(badge);
    }
    nodeTasks.forEach((task, index) => {
      const color = taskColor(task);
      const angle = taskAngles.get(task.task_id) ?? (index / Math.max(1, nodeTasks.length)) * Math.PI * 2 + 0.3;
      taskAngles.set(task.task_id, angle);
      const orbit = new THREE.Group();
      const taskSpin = taskSpins.get(task.task_id) || { y: 0, z: 0.2 };
      orbit.rotation.set(0.5, taskSpin.y, taskSpin.z);
      const marker = new THREE.Mesh(
        new THREE.OctahedronGeometry(0.055, 0),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.96 }),
      );
      marker.position.set(Math.cos(angle) * 0.76, Math.sin(angle) * 0.28, Math.sin(angle) * 0.76);
      marker.userData.entity = { kind: "task", task, node };
      marker.userData.highlight = marker;
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowTexture, color, transparent: true, opacity: 0.24, depthWrite: false, blending: THREE.AdditiveBlending,
      }));
      halo.scale.set(0.5, 0.5, 1);
      marker.add(halo);
      const hit = new THREE.Mesh(
        new THREE.SphereGeometry(0.2, 10, 8),
        new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }),
      );
      hit.position.copy(marker.position);
      hit.userData.entity = { kind: "task", task, node };
      hit.userData.highlight = marker;
      orbit.add(marker, hit);
      group.add(orbit);
      selectable.push(hit);
      taskGroups.set(task.task_id, { marker, orbit, node, task });
      if (TASK_ACTIVE.has(task.status)) {
        const count = quality.mobile ? 14 : 24;
        const positions = new Float32Array(count * 3);
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
        const material = new THREE.PointsMaterial({
          color, size: 0.1, map: glowTexture, transparent: true, opacity: 1,
          depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true,
        });
        const points = new THREE.Points(geometry, material);
        points.frustumCulled = false;
        world.add(points);
        const jitter = Array.from({ length: count }, () => new THREE.Vector3(
          (Math.random() - 0.5) * 0.07, (Math.random() - 0.5) * 0.07, (Math.random() - 0.5) * 0.07,
        ));
        const head = new THREE.Sprite(new THREE.SpriteMaterial({
          map: glowTexture, color, transparent: true, opacity: 0.75,
          depthWrite: false, blending: THREE.AdditiveBlending,
        }));
        head.scale.set(0.45, 0.45, 1);
        world.add(head);
        const curve = curveFor(position.clone().normalize().multiplyScalar(0.62), position, hash(task.task_id));
        taskFlows.push({ points, count, curve, offset: (hash(task.task_id) % 1000) / 1000, speed: 0.22, head, jitter });
      }
    });
  }

  function rebuild(data) {
    for (const child of [...world.children]) {
      world.remove(child);
      disposeObject(child);
    }
    selectable.length = 0;
    nodeGroups.clear();
    taskGroups.clear();
    links.length = 0;
    taskFlows.length = 0;
    messageFlow = null;
    closeDrawer(false);
    selectable.push(coreGroup.userData.hit);

    const nodes = [...(data.nodes || [])].sort((a, b) => String(a.node_id).localeCompare(String(b.node_id)));
    const tasks = visibleTaskList(data.tasks || new Map());
    const layout = nodeLayout(nodes);
    nodes.forEach((node, index) => {
      const position = layout.get(node.node_id);
      addNode(node, position, tasks);
      addLink(node.node_id, position, node.online ? COLORS.online : COLORS.offline, hash(node.node_id) + index);
    });
    lastInteraction = Math.min(lastInteraction, performance.now());
  }

  function setDrawer(entity) {
    const drawer = document.getElementById("viz-drawer");
    const title = document.getElementById("viz-drawer-title");
    const body = document.getElementById("viz-drawer-body");
    if (!drawer || !title || !body) return;
    title.textContent = entity.kind === "core" ? "Core" : entity.kind === "node" ? "Node" : "任务";
    body.replaceChildren();
    const row = (label, value) => {
      const item = document.createElement("div");
      item.className = "viz-row";
      const key = document.createElement("span");
      key.textContent = label;
      const content = document.createElement("strong");
      content.textContent = text(value);
      item.append(key, content);
      body.append(item);
    };
    if (entity.kind === "core") {
      const nodes = state?.nodes || [];
      const tasks = taskList(state?.tasks || new Map());
      row("状态", state?.paused ? "派发已暂停" : "运行中");
      row("设备", `${nodes.filter((node) => node.online).length}/${nodes.length} 在线`);
      row("进行中任务", tasks.filter((task) => TASK_ACTIVE.has(task.status)).length);
      row("当前回复", state?.activeTurn ? "进行中" : "空闲");
      body.append(Object.assign(document.createElement("p"), { className: "viz-note", textContent: "Core 是任务、权限、记忆与费用的唯一权威；场景只展示公开状态，不写入状态。" }));
    } else if (entity.kind === "node") {
      const node = entity.node;
      const tasks = taskList(state?.tasks || new Map()).filter((task) => task.node_id === node.node_id);
      row("状态", node.online ? "在线" : "离线");
      row("Node ID", node.node_id);
      row("工具", node.online ? `${(node.tools || []).length} 项` : "连接后确认");
      row("根范围", (node.roots || []).join("、") || "未报告");
      row("任务", tasks.length ? `${tasks.length} 项` : "无");
      if ((node.tools || []).length) {
        const list = document.createElement("ul");
        list.className = "viz-tools";
        for (const tool of node.tools) {
          const item = document.createElement("li");
          item.textContent = tool;
          list.append(item);
        }
        body.append(list);
      }
      body.append(Object.assign(document.createElement("p"), { className: "viz-note", textContent: "设备权限由 Core 与本机配置共同约束；点击只查看，不执行工具。" }));
    } else {
      const task = entity.task;
      const labels = { awaiting_approval: "等待审批", running: "执行中", succeeded: "已完成", failed: "失败", unknown: "结果未知", cancelled: "已取消" };
      row("状态", labels[task.status] || task.status);
      row("动作", task.request?.intent || "任务");
      row("节点", entity.node?.node_id || task.node_id || "未指定");
      row("任务 ID", task.task_id);
      row("尝试", task.attempts?.length ?? "—");
      body.append(Object.assign(document.createElement("p"), { className: "viz-note", textContent: "任务状态与审批以 Core 为准；这里不提供替代状态机。" }));
    }
    drawer.hidden = false;
    drawer.dataset.kind = entity.kind;
    drawer.classList.add("open");
  }

  function closeDrawer(resetCamera = true) {
    const drawer = document.getElementById("viz-drawer");
    if (drawer) {
      drawer.classList.remove("open");
      drawer.hidden = true;
      drawer.dataset.kind = "";
    }
    if (selected.object) selected.object.scale.setScalar(1);
    selected.entity = null;
    selected.object = null;
    if (resetCamera) {
      desiredTarget.set(0, 0, 0);
      desiredDistance = 7.4;
    }
  }

  function selectObject(object) {
    if (!object) {
      closeDrawer();
      return;
    }
    if (selected.object) selected.object.scale.setScalar(1);
    selected.entity = object.userData.entity;
    const highlight = object.userData.highlight || object;
    highlight.scale.setScalar(1.12);
    selected.object = highlight;
    setDrawer(selected.entity);
    if (selected.entity.kind === "node") {
      const position = nodeGroups.get(selected.entity.node.node_id)?.group.getWorldPosition(new THREE.Vector3());
      if (position) {
        desiredTarget.copy(position).multiplyScalar(0.55);
        desiredDistance = Math.min(desiredDistance, 5.7);
      }
    } else if (selected.entity.kind === "task") {
      const position = nodeGroups.get(selected.entity.node?.node_id)?.group.getWorldPosition(new THREE.Vector3());
      if (position) {
        desiredTarget.copy(position).multiplyScalar(0.55);
        desiredDistance = Math.min(desiredDistance, 5.7);
      }
    } else {
      desiredTarget.set(0, 0, 0);
      desiredDistance = 7.2;
    }
  }

  function pick(clientX, clientY) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(selectable, false)[0];
    if (!hit) return null;
    let object = hit.object;
    while (object && !object.userData.entity) object = object.parent;
    return object || null;
  }

  function onPointerDown(event) {
    lastInteraction = performance.now();
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (event.pointerType === "touch") event.preventDefault();
    if (pointers.size === 1) {
      dragging = true;
      moved = 0;
      lastX = event.clientX;
      lastY = event.clientY;
      thetaVelocity = 0;
      phiVelocity = 0;
    } else if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinchDistance = Math.hypot(a.x - b.x, a.y - b.y);
    }
    renderer.domElement.setPointerCapture?.(event.pointerId);
  }

  function onPointerMove(event) {
    if (!pointers.has(event.pointerId)) {
      if (!dragging) {
        const object = pick(event.clientX, event.clientY);
        renderer.domElement.style.cursor = object ? "pointer" : "grab";
      }
      return;
    }
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    lastInteraction = performance.now();
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const next = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinchDistance) distance = Math.max(4.2, Math.min(12, distance * (pinchDistance / next)));
      pinchDistance = next;
      return;
    }
    if (!dragging) return;
    const dx = event.clientX - lastX;
    const dy = event.clientY - lastY;
    moved += Math.abs(dx) + Math.abs(dy);
    lastX = event.clientX;
    lastY = event.clientY;
    if (moved > 4) {
      theta -= dx * 0.0052;
      phi = Math.max(0.28, Math.min(2.88, phi - dy * 0.0052));
      thetaVelocity = -dx * 0.00055;
      phiVelocity = -dy * 0.00045;
    }
  }

  function onPointerUp(event) {
    if (!pointers.delete(event.pointerId)) return;
    if (pointers.size === 0) {
      if (dragging && moved < 5) selectObject(pick(event.clientX, event.clientY));
      dragging = false;
      pinchDistance = 0;
    }
  }

  function onWheel(event) {
    event.preventDefault();
    desiredDistance = Math.max(4.2, Math.min(12, desiredDistance * Math.exp(event.deltaY * 0.0012)));
    distance = desiredDistance;
    lastInteraction = performance.now();
  }

  function onDoubleClick() {
    theta = 0.42;
    phi = 1.22;
    distance = 7.4;
    desiredDistance = 7.4;
    desiredTarget.set(0, 0, 0);
    closeDrawer(false);
  }

  function removeMessageFlow() {
    if (!messageFlow) return;
    world.remove(messageFlow.points, messageFlow.head);
    disposeObject(messageFlow.points);
    disposeObject(messageFlow.head);
    messageFlow = null;
  }

  function startMessageFlow(nodeId) {
    if (!links.length) return;
    const preferred = nodeId ? links.find((link) => link.nodeId === nodeId) : null;
    const link = preferred || links.find((item) => nodeGroups.get(item.nodeId)?.group.visible !== false) || links[0];
    if (!link) return;
    const count = quality.mobile ? 18 : 34;
    const positions = new Float32Array(count * 3);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    const material = new THREE.PointsMaterial({
      color: 0xdff4ff, size: quality.mobile ? 0.08 : 0.1, map: glowTexture,
      transparent: true, opacity: 0.95, depthWrite: false,
      blending: THREE.AdditiveBlending, sizeAttenuation: true,
    });
    const points = new THREE.Points(geometry, material);
    points.frustumCulled = false;
    const head = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowTexture, color: 0xeaf8ff, transparent: true, opacity: 0.9,
      depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    head.scale.set(0.55, 0.55, 1);
    world.add(points, head);
    messageFlow = { points, count, curve: link.curve, head, nodeId: link.nodeId };
  }

  function syncMessageFlow(data) {
    const key = data.messageKey || data.activeTurn || "";
    if (!key) {
      if (messageFlow && performance.now() > messageExpires) removeMessageFlow();
      return;
    }
    if (!messageFlow) {
      if (key === messageDoneKey) return;
      messageKey = key;
      messageDoneKey = "";
      startMessageFlow(data.nodeId);
      messageExpires = performance.now() + 6000;
    } else if (key !== messageKey || (data.nodeId && messageFlow.nodeId !== data.nodeId)) {
      removeMessageFlow();
      messageKey = key;
      messageDoneKey = "";
      startMessageFlow(data.nodeId);
      messageExpires = performance.now() + 6000;
    }
    if (data.activeTurn) messageExpires = performance.now() + 2500;
    if (!data.activeTurn && messageFlow && performance.now() > messageExpires) {
      removeMessageFlow();
      messageDoneKey = key;
    }
  }

  function updateFlows(now) {
    const time = now * 0.001;
    for (const link of links) {
      const positions = link.flow.geometry.getAttribute("position");
      for (let i = 0; i < link.count; i += 1) {
        const t = ((i / link.count) + time * link.speed + link.offset) % 1;
        const point = link.curve.getPointAt(t);
        const jitter = link.scatter[i];
        positions.setXYZ(i, point.x + jitter.x, point.y + jitter.y, point.z + jitter.z);
      }
      positions.needsUpdate = true;
    }
    for (const flow of taskFlows) {
      const positions = flow.points.geometry.getAttribute("position");
      for (let i = 0; i < flow.count; i += 1) {
        const t = ((i / flow.count) + time * flow.speed + flow.offset) % 1;
        const point = flow.curve.getPointAt(t);
        const jitter = flow.jitter[i];
        positions.setXYZ(i, point.x + jitter.x, point.y + jitter.y, point.z + jitter.z);
      }
      positions.needsUpdate = true;
      const headT = (time * 0.075 + flow.offset) % 1;
      const headPoint = flow.curve.getPointAt(1 - headT);
      flow.head.position.copy(headPoint);
      flow.head.material.opacity = 0.55 + Math.sin(time * 3 + flow.offset * 12) * 0.2;
    }
    if (messageFlow) {
      const positions = messageFlow.points.geometry.getAttribute("position");
      const direction = 1.0;
      for (let i = 0; i < messageFlow.count; i += 1) {
        const t = ((i / messageFlow.count) + time * 0.55) % 1;
        const point = messageFlow.curve.getPointAt(1 - t * direction);
        positions.setXYZ(i, point.x, point.y, point.z);
      }
      positions.needsUpdate = true;
      const headT = (time * 0.38) % 1;
      const headPoint = messageFlow.curve.getPointAt(1 - headT);
      messageFlow.head.position.copy(headPoint);
      messageFlow.head.material.opacity = 0.75 + Math.sin(time * 4.2) * 0.18;
    }
  }

  function animate(now) {
    if (!visible) {
      frame = requestAnimationFrame(animate);
      return;
    }
    const time = now * 0.001;
    if (!dragging) {
      theta += thetaVelocity;
      phi = Math.max(0.28, Math.min(2.88, phi + phiVelocity));
      thetaVelocity *= 0.955;
      phiVelocity *= 0.955;
      if (now - lastInteraction > 3800) theta += 0.00042;
    }
    target.lerp(desiredTarget, 0.045);
    distance += (desiredDistance - distance) * 0.055;
    setCamera();

    const core = coreGroup.userData;
    if (core) {
      core.innerGroup.rotation.y += 0.0017;
      core.innerGroup.rotation.x += 0.0005;
      core.diskGroup.rotation.z += 0.0015;
      core.vortexGroup.rotation.y -= 0.0021;
      core.vortexGroup.rotation.x += 0.0004;
      core.outerGroup.rotation.y -= 0.0009;
      core.outerGroup.rotation.z += 0.0006;
      core.ring.rotation.z += 0.002;
      const breath = 1 + Math.sin(time * 1.1) * 0.025;
      core.corona.scale.set(2.4 * breath, 2.4 * breath, 1);
      core.nucleus.rotation.y += 0.004;
      core.nucleus.rotation.x += 0.002;
    }
    world.rotation.y += 0.00035;
    for (const [nodeId, item] of nodeGroups) {
      const spin = nodeSpins.get(nodeId) || { shellX: 0, shellY: 0, orbitY: 0.2, orbitZ: 0.4 };
      spin.shellX += 0.0015;
      spin.shellY += 0.003;
      spin.orbitZ += 0.004;
      spin.orbitY += 0.002;
      nodeSpins.set(nodeId, spin);
      item.shell.rotation.set(spin.shellX, spin.shellY, 0);
      item.orbitGroup.rotation.set(0.7, spin.orbitY, spin.orbitZ);
      item.glow.material.opacity = 0.28 + Math.sin(time * 1.3 + item.position.x) * 0.08;
    }
    for (const [taskId, item] of taskGroups) {
      const spin = taskSpins.get(taskId) || { y: 0, z: 0.2 };
      spin.y += 0.007;
      spin.z += 0.002;
      taskSpins.set(taskId, spin);
      item.orbit.rotation.set(0.5, spin.y, spin.z);
    }
    updateFlows(now);
    for (const material of particleMaterials) material.uniforms.uTime.value = time;
    if (composer) {
      try {
        composer.value.render();
      } catch {
        renderer.render(scene, camera);
      }
    } else {
      renderer.render(scene, camera);
    }
    frame = requestAnimationFrame(animate);
  }

  renderer.domElement.addEventListener("pointerdown", onPointerDown);
  renderer.domElement.addEventListener("pointermove", onPointerMove);
  renderer.domElement.addEventListener("pointerup", onPointerUp);
  renderer.domElement.addEventListener("pointercancel", onPointerUp);
  renderer.domElement.addEventListener("wheel", onWheel, { passive: false });
  renderer.domElement.addEventListener("dblclick", onDoubleClick);
  const blockContextMenu = (event) => event.preventDefault();
  renderer.domElement.addEventListener("contextmenu", blockContextMenu);
  container.addEventListener("contextmenu", blockContextMenu);
  const resizeObserver = new ResizeObserver(resize);
  resizeObserver.observe(container);
  document.getElementById("viz-drawer-close")?.addEventListener("click", () => closeDrawer());
  resize();
  setCamera();
  frame = requestAnimationFrame(animate);

  const bridge = {
    rotate(dx, dy) {
      theta -= dx * 0.005;
      phi = Math.max(0.28, Math.min(2.88, phi - dy * 0.005));
      lastInteraction = performance.now();
    },
    zoom(delta) {
      desiredDistance = Math.max(4.2, Math.min(12, desiredDistance * Math.exp(delta * 0.0012)));
      lastInteraction = performance.now();
    },
    select(clientX, clientY) {
      selectObject(pick(clientX, clientY));
    },
    reset() {
      theta = 0.42;
      phi = 1.22;
      distance = 7.4;
      desiredDistance = 7.4;
      desiredTarget.set(0, 0, 0);
      closeDrawer(false);
    },
  };

  return {
    update(data) {
      state = data;
      const signature = JSON.stringify({
        nodes: (data.nodes || []).map((node) => [node.node_id, node.online, node.tools, node.roots]),
        tasks: visibleTaskList(data.tasks || new Map()).map((task) => [task.task_id, task.status, task.node_id]),
        active: data.activeTurn || null,
        paused: data.paused || false,
      });
      if (signature !== lastSignature) {
        lastSignature = signature;
        rebuild(data);
      }
      syncMessageFlow(data);
    },
    visible(value) {
      visible = value;
    },
    resize,
    input: bridge,
    close: () => closeDrawer(),
    destroy() {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      renderer.dispose();
      composer?.value.dispose();
      disposeObject(coreGroup);
      disposeObject(world);
      container.replaceChildren();
    },
  };
}

export function getVisualizationBridge(visualization) {
  return visualization?.input || null;
}

export function _colors() {
  return COLORS;
}
