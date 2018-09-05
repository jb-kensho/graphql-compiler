# Copyright 2018-present Kensho Technologies, LLC.
from collections import namedtuple, defaultdict

import six
from sqlalchemy import select, and_, literal_column, cast, String, case, bindparam, Column
from sqlalchemy.sql.elements import BindParameter
from sqlalchemy.sql.util import join_condition

from graphql_compiler.compiler import blocks, expressions
from sqlalchemy.sql import expression as sql_expressions, Join

from graphql_compiler.compiler.helpers import INBOUND_EDGE_DIRECTION
from graphql_compiler.compiler.ir_lowering_sql import constants


# The compilation context holds state that changes during compilation as the tree is traversed
CompilationContext = namedtuple('CompilationContext', [
    'query_path_to_selectable',
    'query_path_to_from_clause',
    'query_path_to_location_info',
    'query_path_to_recursion_columns',
    'query_path_to_filter',
    'query_path_to_output_fields',
    'query_path_field_renames',
    'query_path_to_tag_fields',
    'compiler_metadata',
])


def emit_code_from_ir(sql_query_tree, compiler_metadata):
    """Return a SQLAlchemy query from a tree of  SqlNodes."""
    context = CompilationContext(
        query_path_to_selectable={},
        query_path_to_from_clause={},
        query_path_to_recursion_columns={},
        query_path_field_renames=defaultdict(dict),
        query_path_to_tag_fields=sql_query_tree.query_path_to_tag_fields,
        query_path_to_location_info=sql_query_tree.query_path_to_location_info,
        query_path_to_filter=sql_query_tree.query_path_to_filter,
        query_path_to_output_fields=sql_query_tree.query_path_to_output_fields,
        compiler_metadata=compiler_metadata,
    )
    return _query_tree_to_query(sql_query_tree.root, context, None, None)


def _query_tree_to_query(node, context, recursion_link_column, outer_cte):
    """Recursively converts a SqlNode tree to a SQLAlchemy query."""
    # step 1: Collapse query tree, ignoring recursive nodes
    visited_nodes = _flatten_and_join_nonrecursive_nodes(node, context)
    # step 2: Create the recursive element (only occurs on a recursive call of this function)
    recursion_out_column = _create_recursive_clause(node, context, recursion_link_column, outer_cte)
    # step 3: Materialize query as a CTE.
    cte = _create_query(node, is_final_query=False, context=context).cte()
    # Output fields from individual tables become output fields from the CTE
    _update_context_paths(node, visited_nodes, cte, context)
    # step 4: collapse and return recursive node trees, passing the CTE to the recursive element
    _flatten_and_join_recursive_nodes(node, cte, context)
    if isinstance(node.block, blocks.QueryRoot):
        # filters have already been applied within the CTE, no need to reapply
        return _create_query(node, is_final_query=True, context=context)
    return recursion_out_column


def _flatten_and_join_recursive_nodes(node, cte, context):
    """Join recursive child nodes to parent, flattening child's references."""
    for recursive_node in node.recursions:
        # retrieve the column that will be attached to the recursive element
        recursion_source_column, _ = context.query_path_to_recursion_columns[recursive_node.query_path]
        recursion_sink_column = _query_tree_to_query(
            recursive_node, context, recursion_link_column=recursion_source_column, outer_cte=cte
        )
        _flatten_output_fields(node, recursive_node, context)
        onclause = _get_recursive_join_condition(node, recursive_node, recursion_source_column,
                                                 recursion_sink_column, context)
        _join_nodes(node, recursive_node, onclause, context)


def _update_context_paths(node, visited_nodes, cte, context):
    """Update the visited node's paths to point to the CTE."""
    # this should be where the tag fields get updated, so that they continue to propagate
    context.query_path_to_from_clause[node.query_path] = cte
    for visited_node in visited_nodes:
        context.query_path_to_selectable[visited_node.query_path] = cte


def _flatten_and_join_nonrecursive_nodes(node, context):
    """Join non-recursive child nodes to parent, flattening child's references."""
    # recursively collapse the children's trees
    visited_nodes = [node]
    for child_node in node.children_nodes:
        nodes_visited_from_child = _flatten_and_join_nonrecursive_nodes(child_node, context)
        visited_nodes.extend(nodes_visited_from_child)

    # create the current node's table
    _create_and_reference_table(node, context)
    # ensure that columns required to link recursion are present
    _create_links_for_recursions(node, context)
    for child_node in node.children_nodes:
        _flatten_node(node, child_node, context)
        onclause = _get_join_condition(node, child_node, context)
        _join_nodes(node, child_node, onclause, context)
    return visited_nodes


def _get_node_selectable(node, context):
    """Return the selectable (Table, CTE) of a node."""
    query_path = node.query_path
    selectable = context.query_path_to_selectable[query_path]
    return selectable


def _get_schema_type(node, context):
    """Return the GraphQL type name of a node."""
    query_path = node.query_path
    location_info = context.query_path_to_location_info[query_path]
    return location_info.type.name


def _get_block_direction(block):
    if not isinstance(block, (blocks.Traverse, blocks.Recurse)):
        raise AssertionError()
    return block.direction


def _create_recursive_clause(node, context, out_link_column, outer_cte):
    """Create a recursive clause for a Recurse block."""
    if not isinstance(node.block, blocks.Recurse):
        return None
    if out_link_column is None or outer_cte is None:
        raise AssertionError()
    selectable = _get_node_selectable(node, context)
    edge = _get_join_condition(node, node, context)
    if isinstance(edge, sql_expressions.BinaryExpression):
        out_edge_name = edge.left.name
        in_edge_name = edge.right.name
        base_column_name = out_edge_name
        schema_type = _get_schema_type(node, context)
        recursive_table = context.compiler_metadata.get_table(schema_type).alias()
    elif isinstance(edge, tuple):
        traversal_edge, recursive_table, final_edge = edge
        out_edge_name = final_edge.right.name
        in_edge_name = traversal_edge.right.name
        base_column_name = traversal_edge.left.name
    else:
        raise AssertionError()
    base_column = selectable.c[base_column_name]
    if _get_block_direction(node.block) == INBOUND_EDGE_DIRECTION:
        out_edge_name, in_edge_name = in_edge_name, out_edge_name

    parent_cte_column = outer_cte.c[out_link_column.name]
    anchor_query = (
        select(
            [
                selectable.c[base_column_name].label(out_edge_name),
                selectable.c[base_column_name].label(in_edge_name),
                literal_column('0').label(constants.DEPTH_INTERNAL_NAME),
                cast(base_column, String()).concat(',').label(constants.PATH_INTERNAL_NAME),
            ],
            distinct=True)
        .select_from(
            selectable.join(
                outer_cte,
                base_column == parent_cte_column
            )
        )
    )
    recursive_cte = anchor_query.cte(recursive=True)
    recursive_query = (
        select(
            [
                recursive_table.c[out_edge_name],
                recursive_cte.c[in_edge_name],
                ((recursive_cte.c[constants.DEPTH_INTERNAL_NAME] + 1)
                 .label(constants.DEPTH_INTERNAL_NAME)),
                (recursive_cte.c[constants.PATH_INTERNAL_NAME]
                 .concat(cast(recursive_table.c[out_edge_name], String()))
                 .concat(',')
                 .label(constants.PATH_INTERNAL_NAME)),
            ]
        )
        .select_from(
            recursive_table.join(
                recursive_cte,
                recursive_table.c[in_edge_name] == recursive_cte.c[out_edge_name]
            )
        ).where(and_(
            recursive_cte.c[constants.DEPTH_INTERNAL_NAME] < node.block.depth,
            case(
                [(recursive_cte.c[constants.PATH_INTERNAL_NAME]
                  .contains(cast(recursive_table.c[out_edge_name], String())), 1)],
                else_=0
            ) == 0
        ))
    )
    recursion_combinator = context.compiler_metadata.db_backend.recursion_combinator
    if not hasattr(recursive_cte, recursion_combinator):
        raise AssertionError(
            'Cannot combine anchor and recursive clauses with operation "{}"'.format(
                recursion_combinator
            )
        )
    recursive_query = getattr(recursive_cte, recursion_combinator)(recursive_query)
    from_clause = context.query_path_to_from_clause[node.query_path]
    from_clause = from_clause.join(
        recursive_query,
        selectable.c[base_column_name] == recursive_query.c[out_edge_name]
    )
    from_clause = from_clause.join(
        outer_cte, recursive_query.c[in_edge_name] == parent_cte_column
    )
    context.query_path_to_from_clause[node.query_path] = from_clause
    out_link_column = recursive_query.c[in_edge_name].label(None)
    (in_col, _) = context.query_path_to_recursion_columns[node.query_path]
    context.query_path_to_recursion_columns[node.query_path] = (in_col, out_link_column)
    return out_link_column


def _create_and_reference_table(node, context):
    """Create an aliased table for a node, and update the relevant context."""
    schema_type = _get_schema_type(node, context)
    table = context.compiler_metadata.get_table(schema_type).alias()
    context.query_path_to_from_clause[node.query_path] = table
    context.query_path_to_selectable[node.query_path] = table
    return table


def _flatten_node(node, child_node, context):
    """Flatten a child node's references onto it's parent."""
    _flatten_output_fields(node, child_node, context)
    context.query_path_to_filter[node.query_path].extend(
        context.query_path_to_filter[child_node.query_path]
    )
    del context.query_path_to_filter[child_node.query_path]
    node.recursions.extend(child_node.recursions)


def _flatten_output_fields(parent_node, child_node, context):
    """Flatten child node output fields onto parent node after join operation has been performed."""
    child_output_fields = context.query_path_to_output_fields[child_node.query_path]
    parent_output_fields = context.query_path_to_output_fields[parent_node.query_path]
    for field_alias, (field, field_type, is_renamed) in six.iteritems(child_output_fields):
        parent_output_fields[field_alias] = (field, field_type, is_renamed)
    context.query_path_to_tag_fields[parent_node.query_path].extend(context.query_path_to_tag_fields[child_node.query_path])
    del context.query_path_to_output_fields[child_node.query_path]


def _join_nodes(parent_node, child_node, onclause, context):
    """Join two nodes and update compilation context."""
    location_info = context.query_path_to_location_info[child_node.query_path]
    is_optional = location_info.optional_scopes_depth > 0
    parent_from_clause = context.query_path_to_from_clause[parent_node.query_path]
    child_from_clause = context.query_path_to_from_clause[child_node.query_path]
    if is_optional:
        if isinstance(onclause, tuple):
            parent_to_junction_onclause, junction_table, junction_to_child_onclause = onclause
            parent_from_clause = parent_from_clause.outerjoin(junction_table, onclause=parent_to_junction_onclause)
            parent_from_clause = parent_from_clause.outerjoin(child_from_clause, onclause=junction_to_child_onclause)
        else:
            parent_from_clause = parent_from_clause.outerjoin(child_from_clause, onclause=onclause)
    else:
        if isinstance(onclause, tuple):
            parent_to_junction_onclause, junction_table, junction_to_child_onclause = onclause
            parent_from_clause = parent_from_clause.join(junction_table, onclause=parent_to_junction_onclause)
            parent_from_clause = parent_from_clause.join(child_from_clause, onclause=junction_to_child_onclause)
        else:
            parent_from_clause = parent_from_clause.join(child_from_clause, onclause=onclause)
    context.query_path_to_from_clause[parent_node.query_path] = parent_from_clause
    del context.query_path_to_from_clause[child_node.query_path]


def _create_link_for_recursion(node, recursion_node, context):
    """Ensure that the column necessary to link to a recursion is present in the CTE columns."""
    selectable = _get_node_selectable(node, context)
    # pre-populate the recursive nodes selectable for the purpose of computing the join
    _create_and_reference_table(recursion_node, context)
    edge = _get_join_condition(recursion_node.parent, recursion_node, context)
    # the left side of the expression is the column from the node that is later needed to join to
    recursion_in_col = None
    if isinstance(edge, tuple):
        recursion_on_clause, _, _ = edge
        recursion_in_col = selectable.c[recursion_on_clause.left.name]
    elif isinstance(edge, sql_expressions.BinaryExpression):
        recursion_in_col = selectable.c[edge.left.name]
    else:
        raise AssertionError()
    return recursion_in_col


def _create_links_for_recursions(node, context):
    """Ensure that the columns to link the CTE to the recursive clause are in the CTE's outputs."""
    for recursive_node in node.recursions:
        link_column = _create_link_for_recursion(node, recursive_node, context)
        context.query_path_to_recursion_columns[recursive_node.query_path] = (link_column, None)


def _get_output_columns(node, is_final_query, context):
    """Convert the output fields of a SqlNode to aliased Column objects."""
    output_fields = context.query_path_to_output_fields[node.query_path]
    columns = []
    for field_alias, (field, field_type, is_renamed) in six.iteritems(output_fields):
        selectable = context.query_path_to_selectable[field.location.query_path]
        if is_renamed:
            column = selectable.c[field_alias]
        else:
            field_name = field.location.field
            column = selectable.c[field_name].label(field_alias)
            output_fields[field_alias] = (field, field_type, True)
            context.query_path_field_renames[field.location.query_path][field_name] = field_alias
        columns.append(column)
    # include tags only when we are not outputting the final result
    if not is_final_query:
        for tag_field in context.query_path_to_tag_fields[node.query_path]:
            selectable = context.query_path_to_selectable[tag_field.location.query_path]
            field_name = tag_field.location.field
            column = selectable.c[field_name].label(None)
            columns.append(column)
            context.query_path_field_renames[tag_field.location.query_path][field_name] = column.name
    return columns


def _create_query(node, is_final_query, context):
    """Create a query from a SqlNode.

    If this query is the final query, filters do not need to be applied, and intermediate link
    columns and tag columns do not need to be included in output.
    """
    # filters are computed before output columns, so that tag columns can be resolved before any
    # renames occur for columns involved in output
    filter_clauses = []
    if not is_final_query:
        filter_clauses = [
            _convert_filter_to_sql(filter_block, filter_query_path, context)
            for filter_block, filter_query_path in context.query_path_to_filter[node.query_path]
        ]

    columns = _get_output_columns(node, is_final_query, context)
    if not is_final_query:
        # for every recursion that is a child of this node, include the link column to the child
        # recursion in this node's query's outputs
        for recursion in node.recursions:
            in_col, _ = context.query_path_to_recursion_columns[recursion.query_path]
            columns.append(in_col)
    # If this node is completing a recursion, include the outward column in this node's outputs
    if node.query_path in context.query_path_to_recursion_columns:
        _, out_col = context.query_path_to_recursion_columns[node.query_path]
        columns.append(out_col)

    from_clause = context.query_path_to_from_clause[node.query_path]
    query = select(columns).select_from(from_clause)
    if is_final_query:
        return query
    return query.where(and_(*filter_clauses))


def _convert_filter_to_sql(filter_block, filter_query_path, context):
    """Return the SQLAlchemy expression for a Filter predicate."""
    filter_location_info = context.query_path_to_location_info[filter_query_path]
    filter_selectable = context.query_path_to_selectable[filter_query_path]
    expression = filter_block.predicate
    return _expression_to_sql(expression, filter_selectable, filter_location_info, context)


def _expression_to_sql(expression, selectable, location_info, context):
    """Recursively convert a compiler predicate to it's SQLAlchemy expression representation."""
    if isinstance(expression, expressions.LocalField):
        column_name = expression.field_name
        column = selectable.c[column_name]
        return column
    if isinstance(expression, expressions.Variable):
        variable_name = expression.variable_name
        return bindparam(variable_name)
    if isinstance(expression, expressions.Literal):
        return expression.value
    if isinstance(expression, expressions.ContextField):
        tag_field_name = expression.location.field
        tag_query_path = expression.location.query_path
        tag_column_name = tag_field_name
        if tag_query_path in context.query_path_field_renames:
            if tag_field_name in context.query_path_field_renames[tag_query_path]:
                tag_column_name = context.query_path_field_renames[tag_query_path][tag_field_name]
        tag_selectable = context.query_path_to_selectable[tag_query_path]
        tag_column = tag_selectable.c[tag_column_name]
        return tag_column
    if isinstance(expression, expressions.BinaryComposition):
        sql_operator = constants.OPERATORS[expression.operator]
        left = _expression_to_sql(expression.left, selectable, location_info, context)
        right = _expression_to_sql(expression.right, selectable, location_info, context)
        if sql_operator.cardinality == constants.Cardinality.SINGLE:
            if right is None and left is None:
                raise AssertionError()
            if left is None and right is not None:
                left, right = right, left
            clause = getattr(left, sql_operator.name)(right)
            return clause
        if sql_operator.cardinality == constants.Cardinality.DUAL:
            clause = getattr(sql_expressions, sql_operator.name)(left, right)
            return clause
        if sql_operator.cardinality == constants.Cardinality.MANY:
            if not isinstance(left, BindParameter):
                raise AssertionError()
            if not isinstance(right, Column):
                raise AssertionError()
            # ensure that SQLAlchemy will accept a list/tuple valued parameter for the left side
            left.expanding = True
            clause = getattr(right, sql_operator.name)(left)
            return clause
        raise AssertionError()
    raise AssertionError()


def _get_recursive_join_condition(node, recursive_node, in_column, out_column, context):
    """Determine the join condition to join an outer table to a recursive clause.

    In this case there is a constructed join between the two nodes, not a natural one
    (one that exists via Foreign Keys in the database), so the general _get_join_condition logic
    cannot be used.
    """
    selectable = _get_node_selectable(node, context)
    recursive_selectable = _get_node_selectable(recursive_node, context)
    current_cte_column = selectable.c[in_column.name]
    recursive_cte_column = recursive_selectable.c[out_column.name]
    return current_cte_column == recursive_cte_column


def _get_join_condition(outer_node, inner_node, context):
    """Determine the join condition to join the outer table to the inner table.

    The process to determine this condition is as follows:
    1. Attempt to resolve the join condition as a many-many edge. To do so, one the following must
       hold:
       - There is a table of the correct edge name, eg. for an edge out_Animal_FriendsWith,
         there is a table of name animal_friendswith.
       - there is a table of the correct edge name, with a type suffix. eg. for an edge
         out_Animal_Eats of union type [FoodOrSpecies] that is coerced to Food in the given
         context, there is a table of name animal_eats_food
    2. If there are no results from (1), look for a direct join condition between the two tables.
    3. If the direct join condition is ambiguous, filter raw onclauses to the onclause in the
       correct direction. This onclause must use columns of the correct prefix, eg. for an edge
       Animal_ParentOf, the columns must be animal_id and parentof_id if the direction is "out", or
       parentof_id and animal_id if the direction is "in". This situation arises if there are
       multiple Foreign Keys from a table back onto itself.
    """
    outer_selectable = _get_node_selectable(outer_node, context)
    inner_selectable = _get_node_selectable(inner_node, context)
    # Attempt to resolve via case (1)
    onclause = _try_get_many_to_many_join_condition(outer_node, inner_node, context)
    if onclause is not None:
        return onclause
    # No results, attempt to resolve via case (2)
    onclause = _try_get_direct_join_condition(outer_selectable, inner_selectable)
    if isinstance(onclause, sql_expressions.BinaryExpression):
        return onclause
    if isinstance(onclause, sql_expressions.BooleanClauseList):
        # A BooleanClauseList is only expected when a table is joined directly to itself
        if not _original_tables_equal(outer_selectable, inner_selectable):
            raise AssertionError()
        onclauses = onclause.clauses
        return _filter_onclauses_by_direction(
            onclauses, inner_node, inner_selectable, outer_selectable)
    if onclause is not None:
        raise AssertionError
    edge_name = inner_node.block.edge_name
    column_prefix = edge_name.split('_')[1].lower()
    column_name = u'{column_prefix}_id'.format(column_prefix=column_prefix)
    onclauses = _get_direct_onclauses(outer_selectable, inner_selectable)
    onclauses = [onclause for onclause in onclauses if onclause.right.name == column_name]
    if len(onclauses) == 0:
        raise AssertionError()
    if len(onclauses) == 1:
        return onclauses[0]
    return _filter_onclauses_by_direction(onclauses, inner_node, inner_selectable, outer_selectable)


def _filter_onclauses_by_direction(onclauses, inner_node, inner_selectable, outer_selectable):
    """Filter a list of onclauses to the single onclause that is in the correct direction."""
    block_direction = _get_block_direction(inner_node.block)
    if block_direction == INBOUND_EDGE_DIRECTION:
        inner_selectable, outer_selectable = outer_selectable, inner_selectable
    onclauses = [
        onclause for onclause in onclauses
        if onclause.right.table == inner_selectable and onclause.left.table == outer_selectable
    ]
    if len(onclauses) != 1:
        raise AssertionError()
    return onclauses[0]


def _try_get_many_to_many_join_condition(outer_node, inner_node, context):
    """Attempt to resolve a join condition that uses an underlying many-many junction table."""
    outer_selectable = _get_node_selectable(outer_node, context)
    inner_selectable = _get_node_selectable(inner_node, context)
    edge_name = inner_node.block.edge_name
    type_name = _get_schema_type(inner_node, context)
    short_junction_table_name = u'{junction_table_name}'.format(junction_table_name=edge_name)
    has_short_table_name = context.compiler_metadata.has_table(short_junction_table_name)
    long_junction_table_name = u'{junction_table_name}_{type_name}'.format(
            junction_table_name=edge_name, type_name=type_name
        )
    has_long_table_name = context.compiler_metadata.has_table(long_junction_table_name)
    if not has_long_table_name and not has_short_table_name:
        return None
    if has_long_table_name and has_short_table_name:
        raise AssertionError()
    junction_table_name = (long_junction_table_name
                           if has_long_table_name
                           else short_junction_table_name)
    junction_table = context.compiler_metadata.get_table(junction_table_name).alias()
    outer_column_prefix, inner_column_prefix = edge_name.split('_')
    outer_column_prefix = outer_column_prefix.lower()
    inner_column_prefix = inner_column_prefix.lower()
    direction = _get_block_direction(inner_node.block)
    if direction == INBOUND_EDGE_DIRECTION:
        outer_column_prefix, inner_column_prefix = inner_column_prefix, outer_column_prefix
    junction_table = junction_table.alias()
    outer_to_junction_onclause = _get_junction_join_condition(
        outer_selectable, junction_table, outer_column_prefix)
    junction_to_inner_onclause = _get_junction_join_condition(
        junction_table, inner_selectable, inner_column_prefix)
    return outer_to_junction_onclause, junction_table, junction_to_inner_onclause


def _get_junction_join_condition(outer_selectable, inner_selectable, column_prefix):
    """Get a join condition for joining to a junction table in a many-many edge."""
    join_condition = _try_get_direct_join_condition(outer_selectable, inner_selectable)
    if join_condition is not None:
        return join_condition
    onclauses = _get_direct_onclauses(outer_selectable, inner_selectable)
    if len(onclauses) == 1:
        onclauses = onclauses[0]
        if not isinstance(onclauses, sql_expressions.BinaryExpression):
            raise AssertionError()
        return onclauses
    if len(onclauses) == 2:
        onclauses_in_correct_direction = [
            onclause for onclause in onclauses
            if onclause.right.name.startswith(column_prefix)
        ]
        if len(onclauses_in_correct_direction) != 1:
            raise AssertionError()
        return onclauses_in_correct_direction[0]
    raise AssertionError()


def _try_get_direct_join_condition(outer_selectable, inner_selectable):
    """Attempt to find join condition for joining outer_table to inner_table

    Return None if this clause cannot be uniquely determined.
    """
    try:
        return join_condition(outer_selectable, inner_selectable)
    except constants.UNRESOLVABLE_JOIN_EXCEPTIONS:
        return None


def _get_direct_onclauses(outer_selectable, inner_selectable):
    """Return all direct onclauses between two tables, for joins that cannot be resolved simply."""
    constraints = Join._joincond_scan_left_right(outer_selectable, None, inner_selectable, None)
    onclauses = []
    for constraint_list in six.itervalues(constraints):
        for left, right in constraint_list:
            onclauses.append(left == right)
    return onclauses


def _original_tables_equal(right_table, left_table):
    """Return true if the original tables of two tables are equal.

    In the case of aliased tables, this method will ignore the alias when checking equality.
    """
    if hasattr(right_table, 'original'):
        right_table = right_table.original
    if hasattr(left_table, 'original'):
        left_table = left_table.original
    return right_table == left_table


