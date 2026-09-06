import unittest
import sys
import os

# Run Qt offscreen in headless test environments
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication
from idm_gui.widgets.segment_visualizer import SegmentVisualizerWidget
from idm_gui.widgets.category_tree import CategoryTreeWidget
from idm_gui.widgets.download_table import DownloadTableWidget
from idm_gui.widgets.speed_graph import SpeedGraphWidget

app = QApplication.instance() or QApplication(sys.argv)


class TestGUIWidgets(unittest.TestCase):
    def test_segment_visualizer_widget(self):
        widget = SegmentVisualizerWidget()
        self.assertIsNotNone(widget)

        # Set 4 segments
        segments = [
            {"index": 0, "start_byte": 0, "current_byte": 250, "end_byte": 250, "total_bytes": 250, "status": "completed"},
            {"index": 1, "start_byte": 251, "current_byte": 350, "end_byte": 500, "total_bytes": 250, "status": "downloading"},
            {"index": 2, "start_byte": 501, "current_byte": 501, "end_byte": 750, "total_bytes": 250, "status": "queued"},
            {"index": 3, "start_byte": 751, "current_byte": 751, "end_byte": 1000, "total_bytes": 250, "status": "queued"},
        ]
        widget.set_segments(segments, total_bytes=1000)
        self.assertEqual(len(widget.segments), 4)

    def test_category_tree_widget(self):
        tree = CategoryTreeWidget()
        self.assertIsNotNone(tree)
        selected = []
        tree.category_selected.connect(lambda cat: selected.append(cat))
        tree.select_category("Compressed")
        self.assertIn("Compressed", selected)

    def test_download_table_widget(self):
        table = DownloadTableWidget()
        self.assertIsNotNone(table)
        dls = [
            {
                "id": "dl-1",
                "filename": "archive.zip",
                "total_bytes": 1048576,
                "downloaded_bytes": 524288,
                "status": "downloading",
                "speed": 262144,
                "eta": 2,
                "category": "Compressed",
                "created_at": 1700000000,
                "url": "https://example.com/archive.zip"
            }
        ]
        table.update_downloads(dls)
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 0).text(), "archive.zip")

    def test_download_table_widget_assembling_status(self):
        table = DownloadTableWidget()
        dls = [
            {
                "id": "dl-merging",
                "filename": "video.mp4",
                "total_bytes": 1048576,
                "downloaded_bytes": 1048576,
                "status": "assembling",
                "speed": 0,
                "eta": 0,
                "category": "Video",
                "created_at": 1700000000,
                "url": "https://example.com/video.mp4"
            }
        ]
        table.update_downloads(dls)
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 2).text(), "Assembling")
        pbar = table.cellWidget(0, 3)
        self.assertEqual(pbar.value(), 100)
        self.assertEqual(pbar.format(), "Merging...")
        self.assertEqual(table.item(0, 4).text(), "Merging...")
        self.assertEqual(table.item(0, 5).text(), "Processing")

    def test_speed_graph_widget(self):
        graph = SpeedGraphWidget()
        self.assertIsNotNone(graph)
        graph.add_speed_sample(1024 * 500)
        graph.add_speed_sample(1024 * 1024)
        self.assertEqual(len(graph.samples), 2)


if __name__ == "__main__":
    unittest.main()
