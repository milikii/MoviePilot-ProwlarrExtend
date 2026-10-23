"""Default unittest discovery, including the unchanged pytest-style v2 suite."""
def load_tests(loader, standard_tests, pattern):
    from . import test_prowlarrextend_v3, test_subtitlehunter_v3
    suite = loader.loadTestsFromModule(test_prowlarrextend_v3)
    suite.addTests(loader.loadTestsFromModule(test_subtitlehunter_v3))
    return suite
