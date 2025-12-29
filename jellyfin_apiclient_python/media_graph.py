import asyncio
import contextlib
import typing
import rich
import ubelt as ub
import networkx as nx
import progiter

from jellyfin_apiclient_python.openapi._generated.models.item_fields import ItemFields
from jellyfin_apiclient_python.openapi._generated.types import UNSET, Unset


class MediaGraph:
    """
    Wraps a Jellyfin API client with an interface to walk media folders.

    Builds a graph of all media items in a jellyfin database. A current working
    directory pointer is maintained to allow filesystem like navigation of the
    database.

    Maintains an in-memory graph of the jellyfin database state. This allows
    for efficient client-side queries and exploration, but does take some time
    to construct, and is not kept in sync with the server in the case of
    server-side changes.

    Example:
        >>> from jellyfin_apiclient_python.media_graph import MediaGraph
        >>> import ubelt as ub
        >>> # Given an API client
        >>> MediaGraph.ensure_demo_server(reset=0)
        >>> client = MediaGraph.demo_client()
        >>> # Create the media graph by passing it the client
        >>> self = MediaGraph(client)
        >>> self.walk_config['initial_depth'] = None
        >>> self.setup()
        ...
        >>> # Print the graph at the top level
        >>> self.tree()
        ╟──  f137a2dd21bbc1b99aa5c0f6bf02a805 : 📂 CollectionFolder - Movies
        ╎   ├─╼  826931c9344d013db9db13341db65cce : 🎥 Movie - The great train robbery
        ╎   ├─╼  6144770939e7eeef8d9bd4eb519bf770 : 🎥 Movie - Popeye the Sailor meets Sinbad the Sailor
        ╎   ├─╼  0a8c358081cc4bf1eb74a660ca8616f4 : 🎥 Movie - File:Zur%C3%BCck_in_die_Zukunft_(Film)_01
        ╎   └─╼  32a52b6711776ffeb09b0e737aab5558 : 🎥 Movie - Popeye the Sailor meets Sinbad the Sailor
        ╟──  7e64e319657a9516ec78490da03edccb : 📂 CollectionFolder - Music
        ╎   ├─╼  8288fbf650ae583fc36d715b2c82dff5 : ♬ Audio - Zurück in die Zukunft
        ╎   ├─╼  76ed290f795e4a24a9cceba4aa8bfb33 : ♬ Audio - Heart_Monitor_Beep--freesound.org
        ╎   └─╼  7a7f9d14d80062884dbefd156818b339 : ♬ Audio - Clair De Lune
        ╙──  1071671e7bffa0532e930debee501d2e : 📂 ManualPlaylistsFolder - Playlists
        >>> # Search for items based on name
        >>> found = list(self.find('the'))
        >>> print(f'found = {ub.urepr(found, nl=1)}')
        found = [
            '6144770939e7eeef8d9bd4eb519bf770',
            '32a52b6711776ffeb09b0e737aab5558',
        ]
        >>> # List the folder nodes at the top level
        >>> top_level = self.ls()
        >>> print(f'top_level = {ub.urepr(top_level, nl=1)}')
        top_level = [
            'f137a2dd21bbc1b99aa5c0f6bf02a805',
            '7e64e319657a9516ec78490da03edccb',
            '1071671e7bffa0532e930debee501d2e',
        ]
        >>> # Change the CWD to the music folder
        >>> self.cd('7e64e319657a9516ec78490da03edccb')
        >>> # Print the graph at the CWD
        >>> self.tree()
        ╙──  7e64e319657a9516ec78490da03edccb : 📂 CollectionFolder - Music
            ├─╼  8288fbf650ae583fc36d715b2c82dff5 : ♬ Audio - Zurück in die Zukunft
            ├─╼  76ed290f795e4a24a9cceba4aa8bfb33 : ♬ Audio - Heart_Monitor_Beep--freesound.org
            └─╼  7a7f9d14d80062884dbefd156818b339 : ♬ Audio - Clair De Lune
        >>> # Searching is in the context of the cwd
        >>> found = list(self.find('the'))
        >>> print(f'found = {ub.urepr(found, nl=1)}')
        []
        >>> found = list(self.find('Clair'))
        >>> print(f'found = {ub.urepr(found, nl=1)}')
        found = [
            '7a7f9d14d80062884dbefd156818b339',
        ]
        >>> # Print details about a specific item
        >>> self.print_item('7a7f9d14d80062884dbefd156818b339')
        node=7a7f9d14d80062884dbefd156818b339
        properties = {
            'expanded': False,
        }
        item = {
            'Name': 'Clair De Lune',
            ...
            'Id': '7a7f9d14d80062884dbefd156818b339',
            ...
            'Path': '/media/music/Clair_de_Lune_-_Wright_Brass_-_United_States_Air_Force_Band_of_Flight.mp3',
            ...
            'MediaType': 'Audio',
        }
    """
    def __init__(self, client):
        self.client = client
        self.graph = None
        self.walk_config = {
            'initial_depth': 0,
            'include_collection_types': None,
            'exclude_collection_types': None,
            'perquery_limit': 200,
            'query_attempts': 1,
            'max_concurrency': 10,
        }
        self.display_config = {
            'show_path': False,
        }
        self._cwd = None
        self._cwd_children = None
        self._media_root_nodes = None
        self._DEBUG = False

        from jellyfin_apiclient_python.api import info
        # NOTE: It might not be a great idea to collect all fields by default
        # Things like CumulativeRunTimeTicks might require aggregation
        self.fields = self._coerce_item_fields(info())

        self._user_id = None

    @classmethod
    def ensure_demo_server(cls, reset=False):
        """
        We want to ensure we have a demo server to play with.  We can do this
        with a docker image.

        Requires docker.

        References:
            https://jellyfin.org/docs/general/installation/container#docker
            https://commons.wikimedia.org/wiki/Category:Audio_files
        """
        from jellyfin_apiclient_python.demo.demo_jellyfin_server import DemoJellyfinServerManager
        demoman = DemoJellyfinServerManager()
        demoman.ensure_server(reset=reset)

    @classmethod
    def demo_client(cls):
        """
        Create a client for demos

        Returns:
            jellyfin_apiclient_python.openapi.Jellyfin
        """
        # TODO: Ensure test environment can spin up a dummy jellyfin server.
        from jellyfin_apiclient_python.openapi import Jellyfin

        url = 'http://127.0.0.1:8097'
        username = 'jellyfin'
        password = 'jellyfin'

        client = Jellyfin(base_url=url, username=username, password=password)
        client.login()
        return client

    def tree(self, max_depth=None):
        """
        Print the graph at the current working directory.
        """
        if self._cwd is None:
            self.print_graph(max_depth=max_depth)
        else:
            self.print_graph(sources=[self._cwd], max_depth=max_depth)

    def ls(self):
        """
        List the children of the current working directory (node) in the graph.
        """
        if self._cwd is None:
            return self._media_root_nodes
        else:
            return self._cwd_children

    def cd(self, node):
        """
        Change the cwd to a specific node, and add its children to the graph if
        they have not already been.
        """
        self._cwd = node
        if node is None:
            self._cwd_children = self._media_root_nodes
        else:
            if not self.graph.nodes[node]['item']['IsFolder']:
                raise Exception('can only cd into a folder')
            self.open_node(node, verbose=0)
            self._cwd_children = list(self.graph.succ[node])

    def __truediv__(self, node):
        self.open_node(node, verbose=1)
        return self

    def setup(self):
        """
        Perform an initial walk to build the graph using the user-specified
        configuration.
        """
        if _event_loop_running():
            raise RuntimeError('An event loop is already running. Use "await MediaGraph.async_setup()" instead.')

        _run_coroutine_factory(self.async_setup, reset_async_client=self._reset_async_client)
        return self

    async def async_setup(self):
        """Asynchronous entrypoint for building the media graph."""
        try:
            await self._init_media_folders()
        finally:
            self._update_graph_labels()
        return self

    async def _init_media_folders(self):
        # Initialize Graph
        if self._DEBUG:
            print('Initializing, clearing existing DiGraph')
        client = self.client
        graph = nx.DiGraph()
        self.graph = graph

        include_collection_types = self.walk_config.get('include_collection_types', None)
        exclude_collection_types = self.walk_config.get('exclude_collection_types', None)
        initial_depth = self.walk_config['initial_depth']

        self._media_root_nodes = []

        stats = {
            'node_types': ub.ddict(int),
            'edge_types': ub.ddict(int),
            'nondag_edge_types': ub.ddict(int),
            'total': 0,
            'latest_name': None,
        }

        pman = progiter.ProgressManager()
        with pman:
            if self._DEBUG:
                print('Query top level media folder')
            media_folders = await client.api.library.get_media_folders.asyncio()
            media_folder_dict = self._result_to_dict(media_folders)
            if self._DEBUG:
                print('... top level query complete, scan top level folders')
            items = []
            for folder in pman.progiter(self._coerce_items(media_folder_dict.get('Items', [])), desc='Media Folders'):
                collection_type = folder.get('CollectionType', None)
                if include_collection_types is not None:
                    if collection_type not in include_collection_types:
                        continue
                if exclude_collection_types is not None:
                    if collection_type not in exclude_collection_types:
                        continue

                self._media_root_nodes.append(folder['Id'])
                items.append(folder)
                graph.add_node(folder['Id'], item=folder, properties=dict(expanded=False))

            if self._DEBUG:
                print('... top level scan complete, starting media folder walk.')
            await self._walk_nodes(items, pman, stats, max_depth=initial_depth)

    def open_node(self, node, verbose=0, max_depth=1):
        """
        Add some or all of a node's children to the graph.
        """
        if verbose:
            print(f'open node={node}')
        node_data = self.graph.nodes[node]
        item = node_data['item']
        if _event_loop_running():
            raise RuntimeError('An event loop is already running. Use "await MediaGraph.async_open_node()" instead.')

        _run_coroutine_factory(lambda: self.async_open_node(item, verbose=verbose, max_depth=max_depth), reset_async_client=self._reset_async_client)

    async def async_open_node(self, item, verbose=0, max_depth=1):
        """Asynchronous variant of :func:`open_node`."""
        pman = progiter.ProgressManager(verbose=verbose)
        stats = {
            'node_types': ub.ddict(int),
            'edge_types': ub.ddict(int),
            'nondag_edge_types': ub.ddict(int),
            'total': 0,
            'latest_name': None,
        }
        with pman:
            await self._walk_nodes([item], pman, stats, max_depth=max_depth)
        self._update_graph_labels(sources=[item['Id']])

        if verbose:
            self.print_graph([item['Id']])
            self.print_item(item['Id'])

    async def _walk_nodes(self, items, pman, stats, max_depth=None):
        """
        Iterates through an items children and adds them to the graph until a
        limit is reached.
        """
        client = self.client
        if pman is not None:
            folder_prog = pman.progiter(desc='Walking media tree')
            folder_prog.start()
        else:
            folder_prog = None

        type_add_blocklist = {
            'UserView',
            'CollectionFolder',
        }
        type_recurse_blocklist = {
            'Audio',
            'Episode',
        }

        timer = ub.Timer()
        graph = self.graph

        class StackFrame(typing.NamedTuple):
            item: dict
            depth: int

        walk_semaphore = asyncio.Semaphore(self.walk_config.get('max_concurrency', 10))
        queue: asyncio.Queue[StackFrame | None] = asyncio.Queue()
        for start in items:
            await queue.put(StackFrame(start, 0))

        async def worker():
            while True:
                frame = await queue.get()
                if frame is None:
                    queue.task_done()
                    break

                if max_depth is not None and frame.depth >= max_depth:
                    if folder_prog is not None:
                        folder_prog.step()
                    queue.task_done()
                    continue

                parent = frame.item
                node_data = graph.nodes[parent['Id']]
                node_data['properties']['expanded'] = True

                stats['latest_name'] = parent['Name']
                stats['latest_path'] = parent.get('Path', None)

                parent_id = parent['Id']

                HANDLE_SPECIAL_FEATURES = 1
                if HANDLE_SPECIAL_FEATURES and parent['Type'] in {'Series', 'Season'}:
                    await self._attach_special_features(parent, stats, walk_semaphore)

                # Pagenate children queries
                perquery_limit = self.walk_config['perquery_limit']
                offset = 0
                need_more = True

                fields = self.fields

                while need_more:
                    children = await self._safe_user_items(
                        parent=parent,
                        offset=offset,
                        perquery_limit=perquery_limit,
                        fields=fields,
                        attempts=self.walk_config['query_attempts'],
                        verbose=False,
                        semaphore=walk_semaphore,
                    )

                    children = self._result_to_dict(children)
                    child_items = self._coerce_items(children.get('Items', []))

                    if child_items:
                        stats['total'] += len(child_items)
                        for child in child_items:
                            if child['Id'] in graph.nodes:
                                stats['nondag_edge_types'][(parent['Type'], child['Type'])] += 1
                            else:
                                if child['Type'] not in type_add_blocklist:
                                    stats['edge_types'][(parent['Type'], child['Type'])] += 1
                                    stats['node_types'][child['Type']] += 1

                                    # Add child to graph
                                    graph.add_node(child['Id'], item=child, properties=dict(expanded=False))
                                    graph.add_edge(parent['Id'], child['Id'])

                                    if child['IsFolder'] and child['Type'] not in type_recurse_blocklist:
                                        if max_depth is None or frame.depth + 1 < max_depth:
                                            await queue.put(StackFrame(child, frame.depth + 1))

                    offset += len(child_items)
                    total_record_count = children.get('TotalRecordCount')
                    if total_record_count is None:
                        need_more = len(child_items) >= perquery_limit
                    else:
                        need_more = offset < total_record_count

                    if timer.toc() > 1.1:
                        if pman is not None:
                            pman.update_info(ub.urepr(stats))
                        timer.tic()

                if folder_prog is not None:
                    folder_prog.step()
                queue.task_done()

        workers = [asyncio.create_task(worker()) for _ in range(self.walk_config.get('max_concurrency', 10))]
        await queue.join()
        for _ in workers:
            await queue.put(None)
        await asyncio.gather(*workers)

        if folder_prog is not None:
            folder_prog.stop()

    async def _safe_user_items(self, *, parent, offset, perquery_limit, fields, attempts=1, base_sleep=0.5, verbose=False, semaphore=None):
        """
        Returns children dict, or None if it repeatedly fails.
        """
        import traceback
        import time
        client = self.client
        parent_id = parent['Id']
        parent_name = parent.get('Name', '<no-name>')
        parent_path = parent.get('Path', None)
        total_record_count = parent.get('TotalRecordCount', None)

        if self._DEBUG:
            print(f'Issue query {parent_id=} {offset=} {total_record_count=}: {parent_name=}')

        last_err = None
        for attempt in range(1, attempts + 1):
            try:
                user_id = self._ensure_user_id()
                async with (semaphore or _null_async_context()):
                    children = await client.api.items.get_items.asyncio(
                        parent_id=parent_id,
                        user_id=user_id,
                        recursive=False,
                        fields=fields,
                        limit=perquery_limit,
                        start_index=offset,
                    )
            except RuntimeError as err:
                if 'event loop is closed' in str(err).lower():
                    self._reset_async_client()
                    continue
                last_err = err
                raise
            except Exception as err:
                last_err = err  # NOQA
                # High-signal debug line (includes where you were)
                print(
                    f'[MediaGraph] user_items failed (attempt {attempt}/{attempts}) '
                    f'parent={parent_name!r} id={parent_id} path={parent_path!r} '
                    f'offset={offset} limit={perquery_limit} err={type(err).__name__}: {err}'
                )
                if verbose:
                    traceback.print_exc()

                # Exponential-ish backoff
                time.sleep(base_sleep * (2 ** (attempt - 1)))
            else:
                if self._DEBUG:
                    children_dict = self._result_to_dict(children)
                    total_record_count = children_dict.get('TotalRecordCount')
                    if total_record_count is not None and offset + perquery_limit < total_record_count:
                        print(f'...Got result {total_record_count=}')
                return children

        raise Exception(f'[MediaGraph] giving up on parent={parent_name!r} id={parent_id} after {attempts} attempts')
        # return None

    def _update_graph_labels(self, sources=None):
        """
        Update the rich text representation of select items in the graph.
        """

        glyphs = {
            'FILE_FOLDER': '📁',
            'OPEN_FILE_FOLDER': '📂',
            'FOLD': '🗀',
            'OPEN_FOLDER': '🗁',
            'BEAMED_SIXTEENTH_NOTES': '♬',
            'MOVIE_CAMERA': '🎥',
            'TELEVISION': '📺',
            'FILM_FRAMES': '🎞',
        }

        url = self.client.base_url

        graph = self.graph

        reachable_nodes = reachable(graph, sources)

        # Relabel Graph
        for node in reachable_nodes:
            node_data = graph.nodes[node]
            item = node_data['item']
            properties = node_data['properties']
            expanded = properties.get('expanded', False)
            glyph_key = 'OPEN_FILE_FOLDER' if expanded else 'FILE_FOLDER'
            type_glyph = glyphs[glyph_key]

            if item['Type'] == 'Folder':
                color = 'blue'
            elif item['Type'] == 'CollectionFolder':
                color = 'blue'
            elif item['Type'] == 'Series':
                color = 'cyan'
            elif item['Type'] == 'Season':
                color = 'yellow'
            elif item['Type'] == 'MusicAlbum':
                color = 'cyan'
            elif item['Type'] == 'MusicArtist':
                color = 'cyan'
            elif item['Type'] == 'Episode':
                color = None
                type_glyph = glyphs['TELEVISION']
            elif item['Type'] == 'Video':
                color = None
                type_glyph = glyphs['FILM_FRAMES']
            elif item['Type'] == 'Movie':
                color = None
                type_glyph = glyphs['MOVIE_CAMERA']
            elif item['Type'] == 'Audio':
                color = None
                type_glyph = glyphs['BEAMED_SIXTEENTH_NOTES']
            else:
                color = None

            if color is not None:
                color_part1 = f'[{color}]'
                color_part2 = f'[/{color}]'
            else:
                color_part1 = ''
                color_part2 = ''

            namerep = item['Name']
            path = item.get('Path', None)
            if self.display_config['show_path']:
                if path is not None:
                    namerep = item['Name'] + ' - ' + path
                    # namerep = path

            item_id_link = f'{url}/web/index.html#!/details?id={item["Id"]}'
            item_id_rep = item["Id"]
            item_id_rep = f'[link={item_id_link}]{item_id_rep}[/link]'

            label = f'{color_part1} {item_id_rep} : {type_glyph} {item["Type"]} - {namerep} {color_part2}'
            node_data['label'] = label

    def print(self):
        """
        Alias for :func:`MediaGraph.print_graph`.
        """
        self.print_graph()

    def print_graph(self, sources=None, max_depth=None):
        """
        Prints the current state of the media graph to stdout at a particular
        starting point with a specified depth.
        """
        nx.write_network_text(self.graph, path=rich.print, end='', sources=sources, max_depth=max_depth)

    def print_item(self, node):
        node_data = self.graph.nodes[node]
        item = node_data.get('item', None)
        properties = node_data.get('properties', None)
        rprint(f'node={node}')
        rprint(f'properties = {ub.urepr(properties, nl=1)}')
        rprint(f'item = {ub.urepr(item, nl=1)}')

    def find(self, pattern, data=False, root=None):
        """
        Search for a pattern within the current directory.

        Args:
            pattern (str): text to find in the media name.
            data (bool): if True, also return the data dict
            root (str | None): if specified search from this location,
                if unspecified the cwd is used.

        Yields:
            str | Tuple[str, dict]:
                the id of the found item, or the id and its data if
                data=True
        """
        import networkx as nx
        if root is None:
            root = self._cwd
        graph = self.graph
        if root is None:
            nodes = graph.nodes
        else:
            nodes = nx.descendants(graph, root)
        for node in nodes:
            node_data = graph.nodes[node]
            item = node_data['item']
            name = item['Name']
            # TODO: allow multiple types of patterns (i.e. similar to
            # kwutil.Pattern) to abstract regex, glob, and raw string matching.
            if pattern in name:
                if data:
                    yield node, node_data
                else:
                    yield node

    def find_one(self, pattern, data=False, root=None):
        """
        Find exactly one item matching a pattern.

        Args:
            pattern (str): text to find in the media name.
            data (bool): if True, also return the data dict.
            root (str | None): if specified search from this location,
                if unspecified the cwd is used.

        Returns:
            str | Tuple[str, dict]:
                the unique matching item.

        Raises:
            KeyError:
                if no items match or if multiple items match.
        """
        matches = list(self.find(pattern, data=data, root=root))

        if not matches:
            raise KeyError(f'find_one({pattern!r}) found no matches')

        if len(matches) > 1:
            raise KeyError(
                f'find_one({pattern!r}) found {len(matches)} matches, expected exactly one'
            )

        return matches[0]

    async def _attach_special_features(self, parent, stats, semaphore):
        """
        Attach special features as children of a parent item when available.
        """
        client = self.client
        user_id = self._ensure_user_id()
        async with (semaphore or _null_async_context()):
            try:
                special_features = await client.api.user_library.get_special_features.asyncio(
                    item_id=parent['Id'],
                    user_id=user_id,
                )
            except RuntimeError as err:
                if 'event loop is closed' in str(err).lower():
                    self._reset_async_client()
                    special_features = await client.api.user_library.get_special_features.asyncio(
                        item_id=parent['Id'],
                        user_id=user_id,
                    )
                else:
                    raise

        special_items = self._coerce_items(special_features)
        if not special_items:
            return

        graph = self.graph
        special_features_id = parent['Id'] + '/SpecialFeatures'
        special_features_item = {
            'Name': 'Special Features',
            'Id': special_features_id,
            'Type': 'SpecialFeatures',
            'IsFolder': True,
        }
        if special_features_id not in graph.nodes:
            graph.add_node(special_features_id, item=special_features_item, properties=dict(expanded=True))
            graph.add_edge(parent['Id'], special_features_id)
            stats['edge_types'][(parent['Type'], special_features_item['Type'])] += 1

        for special in special_items:
            stats['edge_types']['SpecialFeatures', special['Type']] += 1
            if special['Id'] in graph.nodes:
                stats['nondag_edge_types'][(parent['Type'], special['Type'])] += 1
            else:
                graph.add_node(special['Id'], item=special, properties=dict(expanded=False))
                graph.add_edge(special_features_id, special['Id'])
                assert not special.get('IsFolder', False)

    def _coerce_item_fields(self, fields):
        """
        Normalize field requests into a list of ItemFields / strings.
        """
        if fields is None:
            return None

        if isinstance(fields, str):
            if ',' in fields:
                fields = [f.strip() for f in fields.split(',') if f.strip()]
            else:
                fields = [fields]

        coerced = []
        for field in fields:
            if isinstance(field, ItemFields):
                coerced.append(field)
                continue
            field_name = str(field)
            try:
                coerced.append(ItemFields(field_name))
            except Exception:
                if self._DEBUG:
                    print(f'[MediaGraph] Unknown ItemField: {field_name!r}, skipping')
                continue
        return coerced

    def _result_to_dict(self, result):
        """
        Convert OpenAPI models to plain dictionaries for easier consumption.
        """
        if result is None or isinstance(result, Unset) or result is UNSET:
            return {}
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {'Items': self._coerce_items(result)}
        if hasattr(result, 'to_dict'):
            return result.to_dict()
        return result

    def _coerce_items(self, items):
        """
        Normalize items from OpenAPI objects / dicts into plain dictionaries.
        """
        if items is None or isinstance(items, Unset) or items is UNSET:
            return []
        if isinstance(items, dict):
            return self._coerce_items(items.get('Items', []))

        normalized = []
        for item in items:
            if item is None or isinstance(item, Unset):
                continue
            if hasattr(item, 'to_dict'):
                item = item.to_dict()
            if isinstance(item, dict):
                normalized.append(self._normalize_item_dict(item))
            else:
                normalized.append(item)
        return normalized

    def _normalize_item_dict(self, item_dict):
        """
        Ensure required keys are present and enums are coerced to strings.
        """
        item = {}
        for key, value in dict(item_dict).items():
            if isinstance(value, Unset):
                continue
            if hasattr(value, 'value'):
                value = value.value
            item[key] = value

        # Map snake_case keys to the legacy camel case that the graph expects
        key_aliases = {
            'id': 'Id',
            'name': 'Name',
            'type': 'Type',
            'is_folder': 'IsFolder',
            'collection_type': 'CollectionType',
            'path': 'Path',
        }
        for src, dst in key_aliases.items():
            if src in item and dst not in item:
                item[dst] = item[src]

        item.setdefault('IsFolder', False)
        return item

    def _ensure_user_id(self):
        """
        Lazily fetch the user id from the OpenAPI client.
        """
        if self._user_id is None:
            # OpenAPI client exposes the user id via property
            self._user_id = self.client.user_id
        return self._user_id

    def _reset_async_client(self):
        """
        Clear any cached httpx.AsyncClient tied to a dead event loop.
        """
        authed = getattr(self.client, 'client', None)
        if authed is None:
            return
        async_client = getattr(authed, '_async_client', None)
        if async_client is None:
            return
        with contextlib.suppress(Exception):
            close = getattr(async_client, 'close', None)
            if callable(close):
                close()
        authed._async_client = None


@contextlib.asynccontextmanager
async def _null_async_context():
    yield


def _event_loop_running():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    else:
        return loop.is_running()


def _run_coroutine_factory(coro_factory, reset_async_client=None):
    """
    Run a coroutine produced by ``coro_factory`` in a fresh event loop.

    This is robust to environments (e.g. IPython) where a default loop may have
    been closed, which would otherwise trigger ``RuntimeError: Event loop is
    closed``. If a loop is already running we surface a friendly error asking
    the caller to ``await`` instead.
    """
    try:
        return asyncio.run(coro_factory())
    except RuntimeError as err:
        msg = str(err).lower()
        if 'asyncio.run() cannot be called from a running event loop' in msg:
            raise RuntimeError('An event loop is already running. Use the async MediaGraph APIs directly.') from err
        if 'event loop is closed' in msg:
            if reset_async_client is not None:
                reset_async_client()
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(coro_factory())
            finally:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
                asyncio.set_event_loop(None)
        raise


def reachable(graph, sources=None):
    if sources is None:
        yield from graph.nodes
    else:
        seen = set()
        for source in sources:
            if source in seen:
                continue
            for node in nx.dfs_preorder_nodes(graph, source):
                seen.add(node)
                yield node


def _find_sources(graph):
    """
    Determine a minimal set of nodes such that the entire graph is reachable
    """
    import networkx as nx
    # For each connected part of the graph, choose at least
    # one node as a starting point, preferably without a parent
    if graph.is_directed():
        # Choose one node from each SCC with minimum in_degree
        sccs = list(nx.strongly_connected_components(graph))
        # condensing the SCCs forms a dag, the nodes in this graph with
        # 0 in-degree correspond to the SCCs from which the minimum set
        # of nodes from which all other nodes can be reached.
        scc_graph = nx.condensation(graph, sccs)
        supernode_to_nodes = {sn: [] for sn in scc_graph.nodes()}
        # Note: the order of mapping differs between pypy and cpython
        # so we have to loop over graph nodes for consistency
        mapping = scc_graph.graph["mapping"]
        for n in graph.nodes:
            sn = mapping[n]
            supernode_to_nodes[sn].append(n)
        sources = []
        for sn in scc_graph.nodes():
            if scc_graph.in_degree[sn] == 0:
                scc = supernode_to_nodes[sn]
                node = min(scc, key=lambda n: graph.in_degree[n])
                sources.append(node)
    else:
        # For undirected graph, the entire graph will be reachable as
        # long as we consider one node from every connected component
        sources = [
            min(cc, key=lambda n: graph.degree[n])
            for cc in nx.connected_components(graph)
        ]
        sources = sorted(sources, key=lambda n: graph.degree[n])
    return sources


def rprint(*args):
    try:
        import rich
        rich.print(*args)
    except ImportError:
        print(*args)
