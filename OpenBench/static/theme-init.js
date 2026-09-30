try {
    const theme = localStorage.getItem('openbench-theme');
    if (theme === 'light' || theme === 'dark')
        document.documentElement.dataset.theme = theme;
} catch (err) {}
