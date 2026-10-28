// Runs only inside the desktop PicPeak processor with its existing private
// PostgreSQL/R2 connection. stdout is a machine-readable stream for Studio.
'use strict';
const {db} = require('/app/src/database/db');
const {getStorage, initStorage} = require('/app/src/services/storage');
const {resolvePhotoStorageKey} = require('/app/src/services/photoResolver');
const {isGalleryAvailable} = require('/app/src/utils/galleryLifecycle');
const {isGalleryHidden} = require('/app/src/utils/revealMode');
const sharp = require('sharp');
const {createHash} = require('node:crypto');
const sourceVersion = (key, stat) => createHash('sha256').update(JSON.stringify([
  key, stat.etag || null, stat.size, stat.mtime || null,
])).digest('hex');

const numericId = value => {
  if (!/^[1-9][0-9]{0,9}$/.test(String(value || ''))) throw Error('Invalid PicPeak ID');
  return Number(value);
};
const parse = value => typeof value === 'string' ? JSON.parse(value) : value;
const bufferOf = async stream => {
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  return Buffer.concat(chunks);
};
const write = data => new Promise((resolve, reject) =>
  process.stdout.write(data, error => error ? reject(error) : resolve()));

const requestedDeadline = Number(process.argv.find(value => value.startsWith('--album-deadline='))?.split('=')[1] || 1800);
const hardDeadline = setTimeout(() => {
  process.stderr.write('PicPeak bridge deadline exceeded\n');
  process.exit(124);
}, Math.max(1, Math.min(requestedDeadline, 7200)) * 1000);

async function main() {
  const mode = process.argv[2];
  if (mode === 'galleries') {
    const events = await db('events').select('id', 'event_name', 'event_date', 'slug', 'is_archived', 'event_type')
      .where('is_archived', false).where('event_type', 'wedding').orderBy('event_date', 'desc');
    const counts = await db('photos').select('event_id').count('* as count').groupBy('event_id');
    const byId = Object.fromEntries(counts.map(row => [row.event_id, Number(row.count)]));
    await write(JSON.stringify(events.map(event => ({...event, photo_count: byId[event.id] || 0}))));
    return;
  }
  const eventId = numericId(process.argv[3]);
  const event = await db('events').where({id: eventId}).first();
  const sourceMode = mode === 'sources';
  const enabled = value => value === true || value === 1 || value === '1';
  const wedding = event?.event_type === 'wedding';
  const galleryAvailable = wedding && isGalleryAvailable(event) && !isGalleryHidden(event);
  if (sourceMode && !galleryAvailable) {
    await write(JSON.stringify({event_id:eventId, available:false, photos:[]}));
    return;
  }
  if (!event || enabled(event.is_archived)) throw Error('PicPeak gallery not found');
  if (!wedding) throw Error('Albums are available for wedding galleries only');
  await initStorage();
  const storage = getStorage();
  if (storage.kind() !== 's3') throw Error('PicPeak R2 storage is required');
  if (sourceMode) {
    const ids = process.argv.slice(4).filter(value => !value.startsWith('--')).map(numericId);
    if (!ids.length || ids.length > 1500) throw Error('Select up to 1500 source photos');
    const rows = await db('photos').where({event_id:eventId}).whereIn('id', ids);
    const photos = [];
    // Sequential HEAD requests bound pressure on the Pi and R2. No idle polling.
    for (const id of ids) {
      const photo = rows.find(row => Number(row.id) === id);
      const key = photo && resolvePhotoStorageKey(event, photo);
      const allowed = photo && photo.processing_status === 'complete' && photo.media_type !== 'video' &&
        (!photo.visibility || photo.visibility === 'visible') && key;
      const stat = allowed ? await storage.stat(key) : null;
      photos.push({photo_id:id, available:!!stat, version:stat ? sourceVersion(key,stat) : null});
    }
    await write(JSON.stringify({event_id:eventId, available:true, slug:event.slug,
      expires_at:event.expires_at, photos}));
    return;
  }
  if (mode === 'original') {
    const photoId = numericId(process.argv[4]);
    const photo = await db('photos').where({id: photoId, event_id: eventId}).first();
    if (!galleryAvailable || !photo || photo.processing_status !== 'complete' || photo.media_type === 'video' ||
        (photo.visibility && photo.visibility !== 'visible')) throw Error('Photo unavailable');
    const key = resolvePhotoStorageKey(event, photo);
    if (!key) throw Error('External photos are not supported by this importer');
    const stat = await storage.stat(key);
    if (!stat || stat.size > 80 * 1024 * 1024) throw Error('Original missing or exceeds 80 MB');
    const expected = process.argv.find(value => value.startsWith('--source-version='))?.split('=')[1];
    if (expected && sourceVersion(key,stat) !== expected) throw Error('Photo changed; check its source again');
    for await (const chunk of await storage.get(key)) await write(chunk);
    const after = await storage.stat(key);
    if (!after || sourceVersion(key,stat) !== sourceVersion(key,after)) throw Error('Photo changed during download; try again');
    return;
  }
  if (mode !== 'previews') throw Error('Unknown bridge operation');
  const photos = await db('photos').where({event_id: eventId, processing_status: 'complete'})
    .whereNot('media_type', 'video').select('id','event_id','filename','original_filename','mime_type',
      'width','height','captured_at','uploaded_at','favorite_count','average_rating','visibility',
      'source_origin','path').orderBy('captured_at','asc');
  const jobs = await db('picpeak_media_jobs').where({event_id: eventId, status: 'ready'})
    .select('photo_id','plan','result');
  const previews = new Map();
  for (const job of jobs) {
    let plan, result;
    try { plan = parse(job.plan); result = parse(job.result); } catch { continue; }
    if (plan?.kind !== 'preview' || !result?.key) continue;
    const previous = previews.get(Number(job.photo_id));
    const width = Number(plan.width) || 1200;
    if (!previous || width > previous.width) previews.set(Number(job.photo_id), {key:result.key,width});
  }
  let skipped = 0, imported = 0;
  for (const photo of photos) {
    try {
      let source = previews.get(Number(photo.id))?.key;
      if (!source || !await storage.stat(source)) source = resolvePhotoStorageKey(event, photo);
      if (!source) throw Error('No managed storage object');
      const raw = await bufferOf(await storage.get(source));
      const preview = await sharp(raw).rotate().resize(1600,1600,{fit:'inside',withoutEnlargement:true})
        .jpeg({quality:85}).toBuffer();
      await write(JSON.stringify({photo: {
        event_id:eventId, photo_id:photo.id, name:photo.original_filename || photo.filename,
        mime_type:photo.mime_type, width:photo.width, height:photo.height,
        captured_at:photo.captured_at || photo.uploaded_at,
        favorite_count:photo.favorite_count || 0, average_rating:photo.average_rating || 0,
        visibility:photo.visibility || 'visible'
      }, preview:preview.toString('base64')}) + '\n');
      imported++;
    } catch (error) {
      skipped++;
      if (skipped <= 20) process.stderr.write(`Skipping PicPeak photo ${photo.id}: ${error.message}\n`);
    }
  }
  await write(JSON.stringify({summary:{event_id:eventId,listing_complete:true,imported,skipped,eligible:photos.length,photos:photos.map(photo=>({photo_id:photo.id,visibility:photo.visibility || 'visible'}))}}) + '\n');
  if (!imported) throw Error('No readable photos found in this PicPeak gallery');
  if (skipped) process.stderr.write(`${imported} imported; ${skipped} unavailable photos skipped\n`);
  if (skipped > 20) process.stderr.write(`${skipped - 20} further unavailable photos skipped\n`);
}
main().then(() => db.destroy()).then(() => clearTimeout(hardDeadline)).catch(error => {
  clearTimeout(hardDeadline);
  process.stderr.write(`${error.message}\n`);
  process.exit(1);
});
