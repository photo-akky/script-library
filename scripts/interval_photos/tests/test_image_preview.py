from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from PIL import Image
    from image_preview import ImageCache, ImagePreviewError, file_version, resolve_image
    from web_app import create_app
except ModuleNotFoundError as exc:
    if exc.name in {'PIL', 'flask'}:
        raise unittest.SkipTest('Web の依存関係をインストールしてください。')
    raise


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'photos'
        self.source.mkdir()
        self.path = self.source / '写真.JPG'
        exif = Image.Exif()
        exif[274] = 6  # Rotate 90 degrees clockwise for display.
        exif[36867] = '2026:10:11 15:08:17'
        Image.new('RGB', (1200, 600), 'green').save(self.path, exif=exif)
        self.cache = ImageCache(self.root / 'cache')
        self.before = (self.path.read_bytes(), self.path.stat().st_mtime_ns)

    def tearDown(self):
        self.assertEqual(self.before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))
        self.tmp.cleanup()

    def test_thumbnail_orientation_dimensions_and_exif_removed(self):
        data = self.cache.get(self.path, 'thumb', file_version(self.path))
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertLessEqual(image.width, 360)
            self.assertLessEqual(image.height, 270)
            self.assertLess(image.width, image.height)
            self.assertEqual(len(image.getexif()), 0)

    def test_larger_preview_and_cache_reuse(self):
        version = file_version(self.path)
        first = self.cache.get(self.path, 'preview', version)
        with patch('image_preview.Image.open', side_effect=AssertionError('cache did not prevent decoding')):
            second = self.cache.get(self.path, 'preview', version)
        self.assertEqual(first, second)
        with Image.open(BytesIO(first)) as image:
            self.assertLessEqual(image.width, 1600)
            self.assertLessEqual(image.height, 1200)
            self.assertGreater(image.height, 270)

    def test_mpo_jpg_uses_primary_frame_and_orientation(self):
        path = self.source / 'SONY.JPG'
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (1200, 600), 'green').save(
            path, format='MPO', save_all=True, exif=exif,
            append_images=[Image.new('RGB', (1200, 600), 'red')])
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        with Image.open(path) as original:
            self.assertEqual(original.format, 'MPO')
            self.assertEqual(original.n_frames, 2)
        for size, limits in [('thumb', (360, 270)), ('preview', (1600, 1200))]:
            data = self.cache.get(path, size, file_version(path))
            with Image.open(BytesIO(data)) as image:
                self.assertEqual(image.format, 'JPEG')
                self.assertLessEqual(image.width, limits[0])
                self.assertLessEqual(image.height, limits[1])
                self.assertLess(image.width, image.height)
                red, green, blue = image.getpixel((image.width // 2, image.height // 2))
                self.assertGreater(green, red + 50)
                self.assertEqual(len(image.getexif()), 0)
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_non_jpeg_with_jpg_extension_is_rejected(self):
        path = self.source / 'not-really-jpeg.JPG'
        Image.new('RGB', (100, 100), 'blue').save(path, format='PNG')
        with self.assertRaisesRegex(ImagePreviewError, 'PNG'):
            self.cache.get(path, 'thumb', file_version(path))

    def test_modified_source_rejected(self):
        version = file_version(self.path)
        wrong = version[:-1] + [version[-1] + 1]
        with self.assertRaisesRegex(ImagePreviewError, '変更'):
            self.cache.get(self.path, 'thumb', wrong)

    def test_cache_count_and_byte_limits(self):
        cache = ImageCache(self.root / 'small-cache', max_entries=1)
        cache.get(self.path, 'thumb', file_version(self.path))
        cache.get(self.path, 'preview', file_version(self.path))
        self.assertEqual(len(list(cache.directory.glob('*.jpg'))), 1)
        tiny = ImageCache(self.root / 'tiny-cache', max_bytes=1)
        self.assertTrue(tiny.get(self.path, 'thumb', file_version(self.path)))
        self.assertEqual(list(tiny.directory.glob('*.jpg')), [])

    def test_only_root_jpg_without_links(self):
        self.assertEqual(resolve_image(self.source, '写真.JPG'), self.path)
        raw = self.source / 'photo.ORF'
        raw.write_bytes(b'RAW')
        (self.source / 'link.JPG').symlink_to(self.path)
        (self.source / 'alias').symlink_to(self.source, target_is_directory=True)
        for value in ['../other.JPG', str(self.path), 'photo.ORF', 'link.JPG', 'alias/写真.JPG']:
            with self.assertRaises(ImagePreviewError):
                resolve_image(self.source, value)

    def test_invalid_image_and_size(self):
        bad = self.source / 'bad.JPG'
        bad.write_bytes(b'not a jpeg')
        with self.assertRaises(ImagePreviewError):
            self.cache.get(bad, 'thumb', file_version(bad))
        with self.assertRaises(ImagePreviewError):
            self.cache.get(self.path, 'original', file_version(self.path))

    def test_image_routes_need_signed_link_and_return_only_resize(self):
        app = create_app({'TESTING': True, 'PHOTO_ROOT': str(self.source), 'PREVIEW_CACHE': str(self.root / 'web-cache'), 'SCAN_TOKEN': 'test'})
        client = app.test_client()
        records = []
        for i in range(5):
            path = self.source / f'{i}.JPG'
            Image.new('RGB', (1000, 600), 'blue').save(path)
            records.append({'SourceFile': str(path), 'DateTimeOriginal': f'2026:10:11 15:09:{i*10:02d}', 'OffsetTimeOriginal': '+09:00'})
        with patch('web_app.read_exif', return_value=records):
            result = client.post('/api/scan', json={}, headers={'X-Scan-Token': 'test'}).get_json()
        file = result['sets'][0]['files'][0]
        response = client.get(file['thumbnail_url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'image/jpeg')
        with Image.open(BytesIO(response.data)) as image:
            self.assertLessEqual(image.width, 360)
        self.assertEqual(client.get(file['preview_url']).status_code, 200)
        self.assertEqual(client.get('/api/image/forged?size=thumb').status_code, 404)
        self.assertEqual(client.get(file['thumbnail_url'].replace('size=thumb', 'size=original')).status_code, 400)
        # Changing the source invalidates an old scan's link rather than showing another photo.
        (self.source / '0.JPG').write_bytes(b'changed')
        self.assertEqual(client.get(file['thumbnail_url']).status_code, 400)


if __name__ == '__main__':
    unittest.main()
