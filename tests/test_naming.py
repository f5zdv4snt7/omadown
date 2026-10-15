import unittest
from pathlib import PurePosixPath

from omadown import naming


class NameSchemeTests(unittest.TestCase):
    def test_default_scheme(self):
        result = naming.render("{title}.{ext}", {"title": "Hello World"}, ext="mp4")
        self.assertEqual(result, "Hello World.mp4")

    def test_missing_title_is_rejected(self):
        with self.assertRaises(naming.NameSchemeError):
            naming.validate_format("{id}.{ext}")

    def test_unknown_token_is_rejected(self):
        with self.assertRaises(naming.NameSchemeError):
            naming.validate_format("{title}.{nope}")

    def test_illegal_characters_are_stripped(self):
        result = naming.render("{title}.{ext}",
                               {"title": 'a/b:c*d?e"f<g>h|i'}, ext="mp4")
        self.assertEqual(result, "a_b_c_d_e_f_g_h_i.mp4")

    def test_slash_inside_a_title_does_not_create_a_directory(self):
        result = naming.render("{title}.{ext}", {"title": "AC/DC live at River Plate"},
                               ext="mp4")
        self.assertEqual(result, "AC_DC live at River Plate.mp4")

    def test_path_separators_are_preserved(self):
        result = naming.render("{uploader}/{title}.{ext}",
                               {"uploader": "Some Channel", "title": "Episode 01"},
                               ext="mkv")
        self.assertEqual(result, "Some Channel/Episode 01.mkv")

    def test_traversal_cannot_escape(self):
        result = naming.render("{title}.{ext}", {"title": "../../etc/passwd"}, ext="mp4")
        path = PurePosixPath(result)
        self.assertFalse(path.is_absolute())
        self.assertNotIn("..", path.parts)
        self.assertNotIn(".", path.parts)
        self.assertNotIn("/", path.name)

    def test_long_names_are_truncated_on_byte_boundary(self):
        result = naming.render("{title}.{ext}", {"title": "\u6c49" * 400}, ext="mp4")
        self.assertLessEqual(len(result.encode("utf-8")), 200)

    def test_unknown_metadata_becomes_placeholder(self):
        result = naming.render("{uploader} - {title}.{ext}", {"title": "T"}, ext="mp4")
        self.assertEqual(result, "unknown - T.mp4")

    def test_ytdlp_template_translation(self):
        template = naming.to_ytdlp_template("{title} [{id}].{ext}")
        self.assertEqual(template, "%(title)s [%(id)s].%(ext)s")

    def test_unique_path(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "movie.mp4"
            first.write_text("x")
            second = naming.unique_path(first)
            self.assertEqual(second.name, "movie (2).mp4")
            second.write_text("x")
            third = naming.unique_path(first)
            self.assertEqual(third.name, "movie (3).mp4")


if __name__ == "__main__":
    unittest.main()
