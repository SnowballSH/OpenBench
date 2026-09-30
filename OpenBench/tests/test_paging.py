from django.test import SimpleTestCase

from OpenBench.utils import getPaging


class Content:
    def __init__(self, total):
        self.total = total

    def count(self):
        return self.total


class PagingTests(SimpleTestCase):
    def paging(self, total, page):
        return getPaging(Content(total), page, 'index')

    def test_nothing_to_page(self):
        self.assertEqual(self.paging(0, 1), (0, 0, {'url': 'index', 'page': 1, 'pages': [], 'prev': 1, 'next': 1}))

    def test_single_page(self):
        start, end, context = self.paging(25, 1)
        self.assertEqual((start, end, context['pages'], context['prev'], context['next']), (0, 25, [1], 1, 1))

    def test_last_partial_page(self):
        start, end, context = self.paging(26, 2)
        self.assertEqual((start, end, context['pages'], context['prev'], context['next']), (25, 26, [1, 2], 1, 2))

    def test_every_row_lands_on_exactly_one_page(self):
        for total in (1, 24, 25, 26, 50, 51, 999, 1000):
            last = self.paging(total, 1)[2]['pages'][-1]
            rows = [row for page in range(1, last + 1) for row in range(*self.paging(total, page)[:2])]
            self.assertEqual(rows, list(range(total)), total)

    def test_far_pages_are_elided(self):
        self.assertEqual(self.paging(1000, 1)[2]['pages'], [1, 2, 3, '...', 38, 39, 40])
        self.assertEqual(self.paging(1000, 20)[2]['pages'], [1, 2, 3, '...', 18, 19, 20, 21, 22, '...', 38, 39, 40])

    def test_neighbours(self):
        context = self.paging(1000, 20)[2]
        self.assertEqual((context['prev'], context['next']), (19, 21))
        context = self.paging(1000, 40)[2]
        self.assertEqual((context['prev'], context['next']), (39, 40))

    def test_pages_past_the_end_show_the_last_page(self):
        start, end, context = self.paging(1000, 99)
        self.assertEqual((start, end, context['page'], context['prev'], context['next']), (975, 1000, 40, 39, 40))

    def test_page_zero_shows_the_first_page(self):
        start, end, context = self.paging(1000, 0)
        self.assertEqual((start, end, context['page']), (0, 25, 1))
