// Original neutral illustrations: no supplier PSD, photograph or scene layer.
// These approximate a closed book, not a supplier's construction or finish.
const scene = aspect => {
  const height = 620 / Math.max(1, aspect);
  const width = height * aspect;
  const x = (1000 - width) / 2, y = (800 - height) / 2;
  return {width: 1000, height: 850, aspect,
    quad: [[x, y], [x + width, y], [x + width, y + height], [x, y + height]]};
};
const SCENES = {
  landscape_40x30: scene(40 / 30), portrait_30x40: scene(30 / 40),
  gobook_landscape_35x25: scene(35 / 25), gobook_portrait_25x35: scene(25 / 35),
};
const imageCache = new Map();
function loadImage(url) {
  if (!imageCache.has(url)) imageCache.set(url, new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('The cover preview could not be loaded'));
    image.src = url;
  }));
  return imageCache.get(url);
}

export function coverCrop(imageWidth, imageHeight, aspect) {
  const [width,height] = imageWidth / imageHeight > aspect
    ? [imageHeight * aspect, imageHeight]
    : [imageWidth, imageWidth / aspect];
  return {left: (imageWidth - width) / 2, top: (imageHeight - height) / 2,
          width, height};
}

export function quadPoint(quad, u, v) {
  const [topLeft, topRight, bottomRight, bottomLeft] = quad;
  return [
    topLeft[0] * (1-u) * (1-v) + topRight[0] * u * (1-v) +
      bottomRight[0] * u * v + bottomLeft[0] * (1-u) * v,
    topLeft[1] * (1-u) * (1-v) + topRight[1] * u * (1-v) +
      bottomRight[1] * u * v + bottomLeft[1] * (1-u) * v,
  ];
}

export function sceneForFormat(designFormat) {
  return SCENES[designFormat] || null;
}

function polygon(ctx, points) {
  ctx.beginPath();
  points.forEach(([x, y], index) => index ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
  ctx.closePath();
}

function drawTriangle(ctx, source, src, dst) {
  const [[sx0,sy0],[sx1,sy1],[sx2,sy2]] = src;
  const [[dx0,dy0],[dx1,dy1],[dx2,dy2]] = dst;
  const determinant = (sx1-sx0)*(sy2-sy0) - (sx2-sx0)*(sy1-sy0);
  const a = ((dx1-dx0)*(sy2-sy0) - (dx2-dx0)*(sy1-sy0)) / determinant;
  const b = ((dy1-dy0)*(sy2-sy0) - (dy2-dy0)*(sy1-sy0)) / determinant;
  const c = ((dx2-dx0)*(sx1-sx0) - (dx1-dx0)*(sx2-sx0)) / determinant;
  const d = ((dy2-dy0)*(sx1-sx0) - (dy1-dy0)*(sx2-sx0)) / determinant;
  const e = dx0-a*sx0-c*sy0, f = dy0-b*sx0-d*sy0;
  const centreX = (dx0+dx1+dx2)/3, centreY = (dy0+dy1+dy2)/3;
  ctx.save();
  ctx.beginPath();
  for (let index=0; index<3; index++) {
    const [x,y] = dst[index];
    const paddedX = x + (x-centreX)*0.025;
    const paddedY = y + (y-centreY)*0.025;
    if (index === 0) ctx.moveTo(paddedX,paddedY); else ctx.lineTo(paddedX,paddedY);
  }
  ctx.closePath(); ctx.clip();
  ctx.setTransform(a,b,c,d,e,f);
  ctx.drawImage(source,0,0);
  ctx.restore();
}

function drawPerspective(ctx, photo, quad, aspect) {
  const source = document.createElement('canvas');
  source.width = 800; source.height = Math.round(800 / aspect);
  const crop = coverCrop(photo.naturalWidth, photo.naturalHeight, aspect);
  source.getContext('2d').drawImage(photo, crop.left, crop.top, crop.width, crop.height,
                                    0, 0, source.width, source.height);
  const columns = 16, rows = 12;
  for (let row=0; row<rows; row++) for (let col=0; col<columns; col++) {
    const u0=col/columns, u1=(col+1)/columns, v0=row/rows, v1=(row+1)/rows;
    const p00=quadPoint(quad,u0,v0), p10=quadPoint(quad,u1,v0);
    const p01=quadPoint(quad,u0,v1), p11=quadPoint(quad,u1,v1);
    const s00=[u0*source.width,v0*source.height], s10=[u1*source.width,v0*source.height];
    const s01=[u0*source.width,v1*source.height], s11=[u1*source.width,v1*source.height];
    drawTriangle(ctx,source,[s00,s10,s11],[p00,p10,p11]);
    drawTriangle(ctx,source,[s00,s11,s01],[p00,p11,p01]);
  }
}

export async function renderMockup(canvas, designFormat, coverUrl) {
  const item = sceneForFormat(designFormat);
  if (!item) throw new Error('Unsupported book illustration');
  const photo = await loadImage(coverUrl);
  if (!canvas.isConnected) return;
  canvas.width = item.width; canvas.height = item.height;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#f1eee8'; ctx.fillRect(0, 0, item.width, item.height);
  const q = item.quad, depth = 18;
  const lower = q.map(([x,y]) => [x + depth, y + depth]);
  ctx.save();
  ctx.shadowColor = '#17232d40'; ctx.shadowBlur = 28;
  ctx.shadowOffsetX = 8; ctx.shadowOffsetY = 12;
  polygon(ctx, lower); ctx.fillStyle = '#d6cfc5'; ctx.fill(); ctx.restore();
  polygon(ctx, [q[3], q[2], lower[2], lower[3]]);
  ctx.fillStyle = '#fffdf7'; ctx.fill();
  drawPerspective(ctx, photo, q, item.aspect);
  polygon(ctx, q); ctx.lineWidth = 3; ctx.strokeStyle = '#243b4a'; ctx.stroke();
  ctx.fillStyle = '#405666'; ctx.font = '20px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText('Illustrative cover · construction and finish may differ', 500, 815);
  canvas.dataset.ready = 'true';
}
