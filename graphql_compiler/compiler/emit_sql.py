from sqlalchemy import select, and_, literal_column, cast, String, case


def emit_code_from_ir(sql_query_tree, compiler_metadata):
    return _query_tree_to_query(sql_query_tree, compiler_metadata)


def _query_tree_to_query(node, compiler_metadata, recursion_in_column=None):
    # step 1: Collapse query tree, ignoring recursive blocks
    _collapse_query_tree(node, compiler_metadata)
    recursion_out_column = None
    # step 2: If the tree rooted at the current node is recursive, create the recursive element
    if node.relation.is_recursive:
        # this node is itself recursive, setup recursion linked to supplied link column
        recursion_out_column = _create_recursive_clause(
            node, compiler_metadata, recursion_in_column
        )
    # step 3: query tree is collapsed, recursion at current node is created
    # materialize and wrap this query in a CTE
    query = _create_base_query(node, compiler_metadata)
    _wrap_query_as_cte(node, query)
    # step 4: collapse and return recursive node trees
    _traverse_recursions(node, compiler_metadata)
    if node.is_tree_root():
        return _create_final_query(node, compiler_metadata)
    return recursion_out_column


def _traverse_recursions(node, compiler_metadata):
    recursive_selections = []
    for recursive_node in node.recursions:
        # retrieve the column that will be attached to the recursive element
        recursion_in_column = node.recursion_to_column[recursive_node]
        recursion_out_column = _query_tree_to_query(
            recursive_node, compiler_metadata, recursion_in_column=recursion_in_column
        )
        recursive_selections.extend(recursive_node.selections)
        _join_to_recursive_node(node, recursion_in_column, recursive_node, recursion_out_column)
    node.selections = node.selections + recursive_selections


def _collapse_query_tree(node, compiler_metadata):
    # recursively collapse the children's trees
    for child_node in node.children_nodes:
        _collapse_query_tree(child_node, compiler_metadata)
    # create the current node's table
    _create_and_reference_table(node, compiler_metadata)
    # ensure that columns required for recursion are present
    _create_links_for_recursions(node, compiler_metadata)
    for child_node in node.children_nodes:
        # pull up the childs SQL blocks
        _pull_up_node_blocks(node, child_node)
        # join to the child
        _join_to_node(node, child_node, compiler_metadata)


def _create_recursive_clause(node, compiler_metadata, out_link_column):
    from_col, to_col = compiler_metadata.get_edge_columns(node.relation)
    base_col = from_col
    base_column = node.table.c[base_col]
    if node.relation.direction == 'in':
        from_col, to_col = to_col, from_col
    recursive_table = compiler_metadata.get_table(node.relation).alias()
    parent_cte_column = node.table.c[out_link_column.name]
    distinct_parent_column_query = select([parent_cte_column.label('link')],
                                          distinct=True).alias()
    anchor_query = (
        select(
            [
                node.table.c[base_col].label(from_col),
                node.table.c[base_col].label(to_col),
                literal_column('0').label('__depth_internal_name'),
                cast(base_column, String()).concat(',').label('path'),
            ],
            distinct=True)
        .select_from(
            node.table.join(
                distinct_parent_column_query,
                base_column == distinct_parent_column_query.c['link']
            )
        )
    )
    recursive_cte = anchor_query.cte(recursive=True)
    recursive_query = (
        select(
            [
                recursive_table.c[from_col],
                recursive_cte.c[to_col],
                (recursive_cte.c['__depth_internal_name'] + 1).label('__depth_internal_name'),
                (recursive_cte.c.path
                 .concat(cast(recursive_table.c[from_col], String()))
                 .concat(',')
                 .label('path')),
            ]
        )
        .select_from(
            recursive_table.join(
                recursive_cte,
                recursive_table.c[to_col] == recursive_cte.c[from_col]
            )
        ).where(and_(
            recursive_cte.c['__depth_internal_name'] < node.relation.recursion_depth,
            case(
                [(recursive_cte.c.path.contains(cast(recursive_table.c[from_col], String())), 1)],
                else_=0
            ) == 0
        ))
    )
    recursion_combinator = compiler_metadata.db_backend.recursion_combinator
    if not hasattr(recursive_cte, recursion_combinator):
        raise AssertionError(
            'Cannot combine anchor and recursive clauses with operation "{}"'.format(
                recursion_combinator
            )
        )
    recursive_query = getattr(recursive_cte, recursion_combinator)(recursive_query)
    node.from_clause = node.from_clause.join(
        recursive_query,
        node.table.c[base_col] == recursive_query.c[from_col]
    )
    out_link_column = recursive_query.c[to_col].label(None)
    node.add_recursive_link_column(recursive_query, out_link_column)
    return out_link_column


def _create_and_reference_table(node, compiler_metadata):
    table = compiler_metadata.get_table(node.relation).alias()
    node.table = table
    node.from_clause = table
    # ensure SQL blocks hold reference to Relation's table
    _update_table_for_blocks(table, node.selections)
    _update_table_for_blocks(table, node.predicates)


def _pull_up_node_blocks(node, child_node):
    node.selections.extend(child_node.selections)
    node.predicates.extend(child_node.predicates)
    node.recursions.extend(child_node.recursions)
    for recursion, link_column in child_node.recursion_to_column.items():
        node.recursion_to_column[recursion] = link_column
    node.link_columns.extend(child_node.link_columns)


def _join_to_node(node, child_node, compiler_metadata):
    # outer table is the current table, inner table is the child's
    on_clause = compiler_metadata.get_on_clause_for_node(child_node)
    if child_node.relation.in_optional:
        node.from_clause = node.from_clause.outerjoin(
            child_node.from_clause, onclause=on_clause
        )
        return
    node.from_clause = node.from_clause.join(
        child_node.from_clause, onclause=on_clause
    )


def _update_table_for_blocks(table, blocks):
    for block in blocks:
        block.table = table


def _create_link_for_recursion(node, recursion, compiler_metadata):
    from_col, _ = compiler_metadata.get_edge_columns(recursion.relation)
    recursion_in_column = node.table.c[from_col]
    node.add_recursive_link_column(recursion, recursion_in_column)


def _create_links_for_recursions(node, compiler_metadata):
    if len(node.recursions) == 0:
        return
    for recursion in node.recursions:
        _create_link_for_recursion(node, recursion, compiler_metadata)


def _join_to_recursive_node(node, recursion_in_column, recursive_node, recursion_out_column):
    current_cte_column = node.table.c[recursion_in_column.name]
    recursive_cte_column = recursive_node.table.c[recursion_out_column.name]
    node.from_clause = node.from_clause.join(
        recursive_node.from_clause, onclause=current_cte_column == recursive_cte_column
    )


def _create_final_query(node, compiler_metadata):
    # no need to adjust predicates, they are already applied
    columns = [compiler_metadata.get_column_for_block(selection) for selection in
               node.selections]
    # no predicates required,  since they are captured in the base CTE
    return _create_query(node, columns, None)


def _wrap_query_as_cte(node, query):
    cte = query.cte()
    node.from_clause = cte
    node.table = cte
    _update_table_for_blocks(cte, node.selections)
    for selection in node.selections:
        # CTE has assumed the alias columns, make sure the selections know that
        selection.rename()


def _create_base_query(node, compiler_metadata):
    selection_columns = [
        compiler_metadata.get_column_for_block(selection) for selection in node.selections
    ]
    selection_columns += node.link_columns
    predicates = [
        compiler_metadata.get_predicate_condition(predicate) for predicate in node.predicates
    ]
    return _create_query(node, selection_columns, predicates)


def _create_query(node, columns, predicates):
    query = select(columns, distinct=True).select_from(node.from_clause)
    if predicates is None:
        return query
    return query.where(and_(*predicates))